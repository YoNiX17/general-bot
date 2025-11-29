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
from datetime import datetime
# Import de la librairie Météo-France
from meteofrance_api import MeteoFranceClient

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
        xp_gain = int((seconds / 60) * 10) 
        if xp_gain > 0: return self.add_xp(user_id, xp_gain)
        self.save_data()
        return False, user["level"]

    def get_leaderboard(self):
        return sorted(self.data.items(), key=lambda x: x[1].get('xp', 0), reverse=True)[:50]

    def set_stats_channels(self, guild_id, category_id, member_id, online_id, voice_id):
        gid = str(guild_id)
        if gid not in self.config: self.config[gid] = {}
        self.config[gid].update({
            "category": category_id,
            "members": member_id,
            "online": online_id,
            "voice": voice_id
        })
        self.save_config()

    # --- GESTION METEO DANS LA DB ---
    def set_meteo_channel(self, guild_id, channel_id):
        gid = str(guild_id)
        if gid not in self.config: self.config[gid] = {}
        self.config[gid]["meteo_channel"] = channel_id
        if "meteo_cities" not in self.config[gid]:
            self.config[gid]["meteo_cities"] = []
        self.save_config()

    def add_meteo_city(self, guild_id, city_name):
        gid = str(guild_id)
        if gid not in self.config: self.config[gid] = {}
        if "meteo_cities" not in self.config[gid]: self.config[gid]["meteo_cities"] = []
        
        if city_name not in self.config[gid]["meteo_cities"]:
            self.config[gid]["meteo_cities"].append(city_name)
            self.save_config()
            return True
        return False

    def remove_meteo_city(self, guild_id, city_name):
        gid = str(guild_id)
        if gid in self.config and "meteo_cities" in self.config[gid]:
            if city_name in self.config[gid]["meteo_cities"]:
                self.config[gid]["meteo_cities"].remove(city_name)
                self.save_config()
                return True
        return False

    def get_meteo_config(self, guild_id):
        gid = str(guild_id)
        return self.config.get(gid, {})

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
        # Client Météo France
        self.meteo_client = MeteoFranceClient()

    async def setup_hook(self):
        # Serveur Web
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
        
        # Lancement des boucles
        if not self.update_stats_loop.is_running():
            self.update_stats_loop.start()
        if not self.meteo_loop.is_running():
            self.meteo_loop.start()

    # --- FONCTIONS METEO ---
    async def fetch_weather(self, city_name):
        """Récupère la météo de manière asynchrone pour ne pas bloquer le bot"""
        def get_data():
            try:
                places = self.meteo_client.search_places(city_name)
                if not places: return None
                place = places[0]
                forecast = self.meteo_client.get_forecast_for_place(place)
                # Tentative récupération pluie (peut échouer selon le lieu)
                try:
                    rain = self.meteo_client.get_rain(place.latitude, place.longitude)
                    next_rain = rain.next_rain_date_locale()
                except:
                    next_rain = None
                return place, forecast, next_rain
            except Exception as e:
                print(f"Erreur météo {city_name}: {e}")
                return None

        return await asyncio.to_thread(get_data)

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
                "id": uid, "name": name, "avatar": avatar,
                "level": data.get("level", 1), "xp": data.get("xp", 0),
                "messages": data.get("messages", 0), "voice_time": data.get("voice_time", 0)
            })
        return web.json_response(json_data, headers={"Access-Control-Allow-Origin": "*"})

    async def web_stats(self, request):
        total_members = sum(g.member_count for g in self.guilds)
        total_online = sum(1 for g in self.guilds for m in g.members if m.status != discord.Status.offline)
        total_voice = sum(len(vc.members) for g in self.guilds for vc in g.voice_channels)
        return web.json_response({
            "guilds": len(self.guilds), "members": total_members,
            "online": total_online, "voice_count": total_voice
        }, headers={"Access-Control-Allow-Origin": "*"})

    # --- EVENTS BOT (XP, VOCAL) ---
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
        if before.channel is None and after.channel is not None:
            self.voice_sessions[member.id] = time.time()
        elif before.channel is not None and after.channel is None:
            if member.id in self.voice_sessions:
                duration = time.time() - self.voice_sessions.pop(member.id)
                levelup, lvl = db.add_voice_time(member.id, duration)
                if levelup:
                    chan = member.guild.system_channel or member.guild.text_channels[0]
                    if chan: await chan.send(f"🎙️ **Vocal Up!** {member.mention} passe niveau **{lvl}** !")
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

                c_members = guild.get_channel(cfg.get("members"))
                c_online = guild.get_channel(cfg.get("online"))
                c_voice = guild.get_channel(cfg.get("voice"))

                if c_members and c_members.name != f"👥 Membres : {member_count}": await c_members.edit(name=f"👥 Membres : {member_count}")
                if c_online and c_online.name != f"🟢 En ligne : {online_count}": await c_online.edit(name=f"🟢 En ligne : {online_count}")
                if c_voice and c_voice.name != f"🔊 En vocal : {voice_count}": await c_voice.edit(name=f"🔊 En vocal : {voice_count}")
            except Exception as e: print(f"Erreur update stats {guild.name}: {e}")

    @tasks.loop(minutes=10)
    async def update_stats_loop(self):
        for guild in self.guilds: await self.update_server_stats(guild)

    # --- BOUCLE METEO (Chaque Heure) ---
    @tasks.loop(minutes=60)
    async def meteo_loop(self):
        print("🌦️ Mise à jour météo...")
        for guild in self.guilds:
            config = db.get_meteo_config(guild.id)
            channel_id = config.get("meteo_channel")
            cities = config.get("meteo_cities", [])
            
            if not channel_id or not cities: continue
            
            channel = guild.get_channel(channel_id)
            if not channel: continue

            # On nettoie les anciens messages du bot (optionnel, pour garder le salon propre)
            try:
                deleted = await channel.purge(limit=10, check=lambda m: m.author == self.user)
            except: pass

            for city in cities:
                data = await self.fetch_weather(city)
                if not data: continue
                
                place, forecast, next_rain = data
                
                # Données actuelles
                current = forecast.current_forecast
                temp = current['T']['value']
                desc = current['weather']['desc']
                icon = "☀️" if "ensoleillé" in desc.lower() else "☁️" if "nuage" in desc.lower() else "🌧️"
                
                # Données Demain
                tomorrow = forecast.daily_forecast[1] # [0] = aujourd'hui, [1] = demain
                t_min = tomorrow['T']['min']
                t_max = tomorrow['T']['max']
                t_desc = tomorrow['weather12H']['desc']

                # Construction Embed
                embed = discord.Embed(title=f"{icon} Météo : {place.name} ({place.admin2})", color=discord.Color.blue())
                embed.add_field(name="🌡️ Actuellement", value=f"**{temp}°C**\n{desc}", inline=True)
                
                if next_rain:
                    embed.add_field(name="☔ Pluie", value=f"Prévue à {next_rain.strftime('%H:%M')}", inline=True)
                else:
                    embed.add_field(name="☔ Pluie", value="Pas de pluie dans l'heure", inline=True)

                embed.add_field(name="📅 Demain", value=f"Min: {t_min}°C | Max: {t_max}°C\n*{t_desc}*", inline=False)
                embed.set_footer(text=f"Mise à jour : {datetime.now().strftime('%H:%M')}")
                
                await channel.send(embed=embed)
                await asyncio.sleep(2) # Pause pour éviter le rate-limit

bot = GeneralBot()

# --- COMMANDES SLASH ---

# ... (Tes commandes précédentes setups_stats, rank, leaderboard, clear, serverinfo restent ici) ...

# --- NOUVELLES COMMANDES METEO ---

@bot.tree.command(name="meteo_setup", description="[Admin] Définit le salon météo")
@app_commands.checks.has_permissions(administrator=True)
async def meteo_setup(interaction: discord.Interaction, salon: discord.TextChannel):
    db.set_meteo_channel(interaction.guild.id, salon.id)
    await interaction.response.send_message(f"✅ Le salon météo est défini sur {salon.mention}. Ajoute des villes avec `/meteo_add`.", ephemeral=True)

@bot.tree.command(name="meteo_add", description="Ajoute une ville à suivre")
@app_commands.checks.has_permissions(administrator=True)
async def meteo_add(interaction: discord.Interaction, ville: str):
    await interaction.response.defer()
    # Vérif si la ville existe
    data = await bot.fetch_weather(ville)
    if not data:
        await interaction.followup.send(f"❌ Ville '{ville}' introuvable sur Météo-France.")
        return
    
    place = data[0]
    if db.add_meteo_city(interaction.guild.id, place.name):
        await interaction.followup.send(f"✅ **{place.name}** ({place.admin2}) ajoutée aux prévisions !")
    else:
        await interaction.followup.send(f"⚠️ **{place.name}** est déjà dans la liste.")

@bot.tree.command(name="meteo_remove", description="Retire une ville")
@app_commands.checks.has_permissions(administrator=True)
async def meteo_remove(interaction: discord.Interaction, ville: str):
    if db.remove_meteo_city(interaction.guild.id, ville):
        await interaction.response.send_message(f"🗑️ **{ville}** retirée des prévisions.")
    else:
        await interaction.response.send_message(f"❌ Cette ville n'était pas suivie.", ephemeral=True)

@bot.tree.command(name="meteo_now", description="Force la mise à jour météo immédiate")
@app_commands.checks.has_permissions(administrator=True)
async def meteo_now(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Mise à jour forcée en cours...", ephemeral=True)
    # On force l'exécution de la boucle (hack pour lancer la tache sans attendre l'heure)
    # Note : Cela ne reset pas le timer de la loop, c'est juste une exécution one-shot
    config = db.get_meteo_config(interaction.guild.id)
    channel_id = config.get("meteo_channel")
    if not channel_id: return
    
    channel = interaction.guild.get_channel(channel_id)
    cities = config.get("meteo_cities", [])
    
    if channel and cities:
        try: await channel.purge(limit=10, check=lambda m: m.author == bot.user)
        except: pass
        
        for city in cities:
            data = await bot.fetch_weather(city)
            if data:
                place, forecast, next_rain = data
                current = forecast.current_forecast
                temp = current['T']['value']
                desc = current['weather']['desc']
                tomorrow = forecast.daily_forecast[1]
                
                embed = discord.Embed(title=f"Météo : {place.name}", color=discord.Color.blue())
                embed.add_field(name="Actuellement", value=f"{temp}°C - {desc}")
                embed.add_field(name="Demain", value=f"{tomorrow['T']['min']}/{tomorrow['T']['max']}°C - {tomorrow['weather12H']['desc']}")
                await channel.send(embed=embed)

# ... (Reste de tes commandes existantes) ...
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
    top_users = db.get_leaderboard() 
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
