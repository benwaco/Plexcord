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
DISCORD_ADMIN_ROLE_ID = os.getenv("DISCORD_ADMIN_ROLE_ID")

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
    await ctx.respond(f"Pong! {int(bot.latency * 1000)}ms", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID])
async def add(ctx, email: str):
    try:
        plex.myPlexAccount().inviteFriend(email, plex, allowSync=True, allowCameraUpload=True, filterMovies=None, filterTelevision=None, filterMusic=None, allowChannels=None)
        await ctx.respond(f"Invited {email} to plex server", ephemeral=True)
        # if successful add the email, discord id and share status to the database
        await db_plex["plex"].insert_one({
            "email": email,
            "discord_id": ctx.author.id,
            "share_status": "pending",
            "archived": False
        })
    except Exception as e:
        print(e)
        await ctx.respond(f"Error adding {email} to plex server", ephemeral=True)

# make a slash command to remove a user from plex (based on provided discord id) and change the share status to 'manually removed' in the database
@bot.slash_command(guild_ids=[GUILD_ID])
async def remove(ctx, discord_id: str):
    discord_id = int(discord_id)
    try:
        # user_data = await db_plex["plex"].find_one({"discord_id": discord_id}) change to check if it is not archived
        user_data = await db_plex["plex"].find_one({"discord_id": discord_id, "archived": False})
        
        if user_data is None:
            await ctx.respond(f"User with Discord ID {discord_id} not found in the database or user is archived.", ephemeral=True)
            return
        
        email = user_data["email"]
        print(email)
        # remove the user from plex
        plex.myPlexAccount().removeFriend(email)
        # update the database
        await db_plex["plex"].update_one({"discord_id": discord_id}, {"$set": {"share_status": "removed manually"}})
        await db_plex["plex"].update_one({"discord_id": discord_id}, {"$set": {"archived": True}})
        await ctx.respond(f"Removed {email} from plex server", ephemeral=True)
    except Exception as e:
        print(e)
        await ctx.respond(f"Error removing user from plex server, {e}", ephemeral=True)

# make a basic slash command for customers to view their current share status, print their plex email and share status from the database. tell user if they are archived and explain what archived means
@bot.slash_command(guild_ids=[GUILD_ID])
async def status(ctx):
    try:
        user_data = await db_plex["plex"].find_one({"discord_id": ctx.author.id})
        if user_data is None:
            await ctx.respond(f"You are not in the database.", ephemeral=True)
            return
        email = user_data["email"]
        share_status = user_data["share_status"]
        archived = user_data["archived"]
        if archived:
            await ctx.respond(f"Your email is {email}, and your share status is {share_status}. You are archived. This means that you have been removed from the server manually or we have removed you due to non-payment. If you wish to be re-added, please contact us.", ephemeral=True)
        else:
            await ctx.respond(f"Your email is {email}, and your share status is {share_status}.", ephemeral=True)
    except Exception as e:
        print(e)
        await ctx.respond(f"Error retrieving your share status, {e}", ephemeral=True)

@bot.slash_command(guild_ids=[GUILD_ID])
async def lookup(ctx, discord_id: str):
    discord_id = int(discord_id)
    try:
        user_data = await db_plex["plex"].find_one({"discord_id": discord_id})
        if user_data is None:
            await ctx.respond(f"{discord_id} is not in the database.", ephemeral=True)
            return
        email = user_data["email"]
        share_status = user_data["share_status"]
        archived = user_data["archived"]
        if archived:
            await ctx.respond(f"{discord_id}'s email is {email}, and their share status is {share_status}. They are archived. This means that they have been removed from the server manually or we have removed them due to non-payment. If they wish to be re-added, they should contact us.", ephemeral=True)
        else:
            await ctx.respond(f"{discord_id}'s email is {email}, and their share status is {share_status}.", ephemeral=True)
    except Exception as e:
        print(e)
        await ctx.respond(f"Error retrieving {discord_id}'s share status, {e}", ephemeral=True)

bot.run(DISCORD_TOKEN)