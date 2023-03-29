import dotenv
import os
import discord
import motor.motor_asyncio
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

# Load Environment Variables
dotenv.load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
PLEX_SERVER_URL = os.getenv("PLEX_SERVER_URL")
PLEX_TOKEN = os.getenv("PLEX_SERVER_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
MONGODB_URL = os.getenv("MONGODB_URL")
PLEX_USERNAME = os.getenv("PLEX_USERNAME")
PLEX_PASSWORD = os.getenv("PLEX_PASSWORD")
PLEX_SERVER_NAME = os.getenv("PLEX_SERVER_NAME")

# Setup Plex Server
try:
    print("Connecting to Plex Server...")
    account = MyPlexAccount(PLEX_USERNAME, PLEX_PASSWORD)
    plex = account.resource(PLEX_SERVER_NAME).connect()  # returns a PlexServer instance
except Exception as e:
    print(e)


# Setup MongoDB with motor
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
db_plex = client["pycord"]
db_payments = client["pycord"]

# Setup Discord Bot
print("Connecting to Discord...")
intents = discord.Intents.all()
bot = discord.Bot(intents=intents)
@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')

# Setup Slash Commands
@bot.slash_command(guild_ids=[GUILD_ID])
async def ping(ctx):
    await ctx.respond(f"Pong! {int(bot.latency * 1000)}ms")

@bot.slash_command(guild_ids=[GUILD_ID])
async def add(ctx, email: str):
    try:
        plex.myPlexAccount().inviteFriend(email, plex, allowSync=True, allowCameraUpload=True, filterMovies=None, filterTelevision=None, filterMusic=None, allowChannels=None)
        await ctx.respond(f"Invited {email} to plex server")
        # if successful add the email, discord id and share status to the database
        await db_plex["plex"].insert_one({
            "email": email,
            "discord_id": ctx.author.id,
            "share_status": "pending",
            "archived": False
        })
    except Exception as e:
        print(e)
        await ctx.respond(f"Error adding {email} to plex server")

# make a slash command to remove a user from plex (based on provided discord id) and change the share status to 'manually removed' in the database
@bot.slash_command(guild_ids=[GUILD_ID])
async def remove(ctx, discord_id: str):
    discord_id = int(discord_id)
    try:
        # user_data = await db_plex["plex"].find_one({"discord_id": discord_id}) change to check if it is not archived
        user_data = await db_plex["plex"].find_one({"discord_id": discord_id, "archived": False})
        
        if user_data is None:
            await ctx.respond(f"User with Discord ID {discord_id} not found in the database or user is archived.")
            return
        
        email = user_data["email"]
        print(email)
        # remove the user from plex
        plex.myPlexAccount().removeFriend(email)
        # update the database
        await db_plex["plex"].update_one({"discord_id": discord_id}, {"$set": {"share_status": "removed manually"}})
        await db_plex["plex"].update_one({"discord_id": discord_id}, {"$set": {"archived": True}})
        await ctx.respond(f"Removed {email} from plex server")
    except Exception as e:
        print(e)
        await ctx.respond(f"Error removing user from plex server, {e}")


bot.run(DISCORD_TOKEN)