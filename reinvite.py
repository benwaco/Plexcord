import datetime
import math
import os
import sys

import discord
import dotenv
import motor.motor_asyncio
import yaml
from async_stripe import stripe
from discord.ext import tasks
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
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")
DISCORD_ADMIN_ID = os.getenv("DISCORD_ADMIN_ID")
STATS = os.getenv("STATS")
STATS_CHANNEL_ID = os.getenv("STATS_CHANNEL_ID")


def load_plans():
    with open("plans.yml", "r") as file:
        plans_data = yaml.safe_load(file)
    return plans_data["plans"]


plans = load_plans()

role_ids = []

for plan in plans:
    role_ids.append(plan["role_id"])

# Setup Stripe
stripe.api_key = STRIPE_API_KEY


# Setup Plex Server
try:
    print("Connecting to Plex... This may take a few seconds.")
    account = MyPlexAccount(PLEX_USERNAME, PLEX_PASSWORD)
    plex = account.resource(PLEX_SERVER_NAME).connect()  # returns a PlexServer instance
    print("Connected to Plex Server")

except Exception as e:
    print(e)
    sys.exit()


# setup sections

sections_standard = []
sections_4k = []
sections_all = []

sections_movies = []
sections_tv = []

for section in plex.library.sections():
    if "4K" not in section.title:
        sections_standard.append(section)
    sections_all.append(section)

    if section.type == "movie":
        sections_movies.append(section)
    elif section.type == "show":
        sections_tv.append(section)


# Setup MongoDB with motor
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URL)
db_plex = client["pycord"]
db_payments = client["pycord"]
db_subscriptions = client["pycord"]


users = db_plex["plex"].find().to_list(length=None)

def add_to_plex(email, discord_id, plan_name):
    try:
        # find "downloads_enabled" and "4k_enabled" in the plans value and set them to the vars here
        selected_plan = next(
            (plan for plan in plans if plan["name"] == plan_name), None
        )
        downloads_enabled = selected_plan["downloads_enabled"]
        enabled_4k = selected_plan["4k_enabled"]
        if enabled_4k:
            add_sections = sections_all
        else:
            add_sections = sections_standard
        plex.myPlexAccount().inviteFriend(
            email,
            plex,
            allowSync=downloads_enabled,
            sections=add_sections,
        )
        # If successful, add the email, discord id and share status to the database
        return True
    except Exception as e:
        return e

for user in users:
    discord_id = user["discord_id"]
    email = user["email"]
    plan_name = user["plan_name"]
    print(add_to_plex(email, discord_id, plan_name), "for", email, "with plan", plan_name, "and discord id", discord_id)