import datetime
import math
import os
import sys
import re

import discord
import dotenv
import motor.motor_asyncio
import yaml
from async_stripe import stripe
from discord.ext import tasks
from plexapi.myplex import MyPlexAccount

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

VALID_SUBTITLE_EXTENSIONS = [".srt", ".smi", ".ssa", ".ass", ".vtt"]

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

# Setup Discord Bot
print("Connecting to Discord... This may take a few seconds.")
intents = discord.Intents.all()
bot = discord.Bot(intents=intents)


@bot.event
async def on_ready():
    bot.add_view(PlanView())
    bot.add_view(
        ManageSubscriptionButton()
    )  # Registers a View for persistent listening
    subscriptionCheckerLoop.start()
    if STATS == "true":
        stats_update.start()
    print(f"We have logged in as {bot.user}")


async def add_to_plex(email, discord_id, plan_name):
    test = await db_plex["plex"].find_one({"email": email})
    if test is not None:
        return "Your Plex account is already in the database."
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
        await db_plex["plex"].insert_one(
            {
                "email": email,
                "discord_id": discord_id,
                "expiration_date": None,
                "plan_id": None,
                "plan_name": None,
                "sent_notifications": [],
                "expired": False,
            }
        )
        return True
    except Exception as e:
        return e


# Setup Slash Commands


# User
# @bot.slash_command(guild_ids=[GUILD_ID])
# async def ping(ctx):
#     await ctx.respond(f"Pong! {int(bot.latency * 1000)}ms", ephemeral=True)


async def donate(email: str, stripe_price_id: str, discord_author_id: str, plan_name):
    try:
        # Check if the user already has a pending invoice
        existing_payment = await db_payments["payments"].find_one(
            {"discord_id": discord_author_id, "active": True}
        )
        if existing_payment is not None:
            return (
                f"You already have a pending invoice for the plan **{existing_payment['plan_name']}**. Please pay it at {existing_payment['invoice_url']} or use the cancel button to cancel the existing invoice before creating a new one.  **Click the green Complete Payment button after paying**",
            )

        # Create Stripe customer
        customer = await stripe.Customer.create(email=email)
        # Create Stripe invoice
        invoice_item = await stripe.InvoiceItem.create(
            customer=customer.id,
            price=stripe_price_id,  # Replace with your Stripe price ID
        )

        invoice = await stripe.Invoice.create(
            customer=customer.id,
            auto_advance=True,
            pending_invoice_items_behavior="include",
        )

        # Finalize the invoice
        finalised_invoice = await stripe.Invoice.finalize_invoice(invoice.id)

        # Log unpaid invoice to the "payments" MongoDB collection
        await db_payments["payments"].insert_one(
            {
                "discord_id": discord_author_id,
                "email": email,
                "invoice_id": finalised_invoice.id,
                "paid": False,
                "invoice_url": finalised_invoice.hosted_invoice_url,
                "active": True,
                "plan_name": plan_name,
                "plan_id": stripe_price_id,
            }
        )

        # Send the invoice URL to the user
        # Direct message the user the link as well
        return finalised_invoice.hosted_invoice_url
    except Exception as e:
        return f"Error creating your subscription, {e}"


async def add_time(discord_id):
    plex_data = await db_plex["plex"].find_one({"discord_id": discord_id})
    email, stripe_price_id, plan_name = (
        plex_data["email"],
        plex_data["plan_id"],
        plex_data["plan_name"],
    )

    return await donate(email, stripe_price_id, discord_id, plan_name)


class EmailModal(discord.ui.Modal):
    def __init__(self, plan, plan_name, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_item(discord.ui.InputText(label="Email Address"))
        self.plan = plan
        self.plan_name = plan_name

    async def callback(self, interaction: discord.Interaction):
        email = self.children[0].value
        donate_return = await donate(
            email,
            self.plan,
            interaction.user.id,
            plan_name=self.plan_name,
        )
        if donate_return[0].startswith("You"):
            await interaction.response.send_message(
                donate_return[0],
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Please pay the invoice at the following URL: {donate_return}, the link has also been messaged to you. **Click the green Complete Payment button after paying**",
            ephemeral=True,
        )
        await interaction.user.send(
            f"Please pay the invoice at the following URL: {donate_return}.  **Click the green Complete Payment button after paying**"
        )


@bot.slash_command(guild_ids=[GUILD_ID])
async def send_subscription_menu(ctx):
    if int(DISCORD_ADMIN_ROLE_ID) not in [role.id for role in ctx.author.roles]:
        await ctx.respond(
            "You do not have permission to use this command.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Manage Subscription",
        description="Click the button below to manage your subscription.",
        color=discord.Color.blue(),
    )
    await ctx.send(embed=embed, view=ManageSubscriptionButton())

    await ctx.respond(
        f"Sent the embed, persistent status: {ManageSubscriptionButton.is_persistent(ManageSubscriptionButton())}",
        ephemeral=True,
    )


class ManageSubscriptionButton(
    discord.ui.View
):  # Create a class called MyView that subclasses discord.ui.View
    def __init__(self):
        super().__init__(timeout=None)  # timeout of the view must be set to None

    @discord.ui.button(
        label="Manage Subscription",
        row=0,
        style=discord.ButtonStyle.primary,
        custom_id="manage_subscription",
    )
    async def first_button_callback(self, button, interaction):
        subscription_info = await checkSubscriptionInfo(interaction.user.id)
        expiration_date = subscription_info[0]
        plan_name = subscription_info[1]
        embed = discord.Embed(
            title="Manage Subscription",
            description="Use the buttons below to modify your subscription.",
        )
        remaining_time = expiration_date - datetime.datetime.now()
        remaining_days = remaining_time.days

        embed.add_field(name="Plan", value=plan_name)
        embed.add_field(
            name="Expiration Date", value=expiration_date.strftime("%B %d, %Y")
        )
        embed.add_field(name="Remaining Time", value=f"{remaining_days} days")
        await interaction.response.send_message(
            embed=embed,
            view=ManageSubscriptionMenu(discord_id=interaction.user.id),
            ephemeral=True,
        )


class ManageSubscriptionMenu(discord.ui.View):
    def __init__(self, discord_id, *args, **kwargs) -> None:
        super().__init__(timeout=None, *args, **kwargs)
        self.discord_id = discord_id

    # i wanna send an embed with the users remaining subscription time here?

    @discord.ui.button(
        label="Add Time",
        row=0,
        style=discord.ButtonStyle.primary,
        custom_id="add_time",
    )
    async def first_button_callback(self, button, interaction):
        donate_return = await add_time(self.discord_id)
        if donate_return[0].startswith("You"):
            await interaction.response.send_message(
                donate_return[0],
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Please pay the invoice at the following URL: {donate_return}, the link has also been messaged to you.  **Click the green Complete Payment button after paying**",
            ephemeral=True,
        )
        await interaction.user.send(
            f"Please pay the invoice at the following URL: {donate_return}.  **Click the green Complete Payment button after paying**"
        )

    @discord.ui.button(
        label="Complete Payment",
        row=0,
        style=discord.ButtonStyle.green,
        custom_id="complete_payment",
    )
    async def second_button_callback(self, button, interaction):
        await interaction.response.send_message(
            await complete_payment(interaction.user.id), ephemeral=True
        )

    @discord.ui.button(
        label="Cancel Payment",
        row=0,
        style=discord.ButtonStyle.red,
        custom_id="cancel_payment",
    )
    async def third_button_callback(self, button, interaction):
        await interaction.response.send_message(
            await cancel_payment(interaction.user.id), ephemeral=True
        )


# make a list of role ids from plans yml


@bot.slash_command(description="Upload a subtitle to Plex", guild_ids=[GUILD_ID])
async def upload_subtitles(
    ctx,
    media_url: discord.Option(discord.SlashCommandOptionType.string, description="Copy the URL from Plex Web."),
    subtitle_file: discord.Option(discord.SlashCommandOptionType.attachment, description=f"Upload a subtitle file with one of the following extensions: {', '.join(VALID_SUBTITLE_EXTENSIONS)}"),
):
    if not any(subtitle_file.filename.lower().endswith(ext) for ext in VALID_SUBTITLE_EXTENSIONS):
        await ctx.respond(
            f"Invalid subtitle file type. The file must be one of the following types: {', '.join(VALID_SUBTITLE_EXTENSIONS)}.",
            ephemeral=True,
        )
        await contactAdmin(f'{ctx.author.mention} tried to upload a subtitle file with an invalid extension: {subtitle_file.filename}.')
        return
    pattern = r"metadata%2F(\d+)&context"
    match = re.search(pattern, media_url)
    if not match:
        await ctx.respond(
            "Invalid media URL, please copy the url on the media page.",
            ephemeral=True,
        )
        return
    media_id = match.group(1)
    media = plex.fetchItem(int(media_id))

    # Check if the directory exists, and if not, create it.
    directory = "./subtitles/"
    if not os.path.exists(directory):
        os.makedirs(directory)

    await subtitle_file.save(f"{directory}{subtitle_file.filename}")
    subtitle_path = f"{directory}{subtitle_file.filename}"
    media.uploadSubtitles(subtitle_path)
    return await ctx.respond(f"Uploaded subtitle {subtitle_file.filename}.")



@bot.slash_command(guild_ids=[GUILD_ID])
async def send_plans_embed(ctx):
    if int(DISCORD_ADMIN_ROLE_ID) not in [role.id for role in ctx.author.roles]:
        await ctx.respond(
            "You do not have permission to use this command.", ephemeral=True
        )
        return

    embed = discord.Embed()
    for plan in plans:
        embed.add_field(
            name=plan["name"],
            value=(
                f"Price: ${plan['price']}\n"
                f"Concurrent Streams: {plan['concurrent_streams']}\n"
                f"Downloads Enabled: {'Yes*' if plan['downloads_enabled'] else 'No'}\n"
                f"4K Enabled: {'Yes' if plan['4k_enabled'] else 'No'}\n"
            ),
            inline=True,
        )
    embed.add_field(
        name="",
        value="*Plex accounts created after August 1, 2022 require a Plex Pass to utilize downloads. For more information, see https://support.plex.tv/articles/downloads-sync-faq/.",
        inline=False,
    )
    await ctx.send(embed=embed)


async def checkSubscriptionInfo(discord_id):
    plex_data = await db_plex["plex"].find_one({"discord_id": discord_id})
    return plex_data["expiration_date"], plex_data["plan_name"]


async def lookup_subscription(discord_id):
    user_data = await db_payments["plex"].find_one({"discord_id": discord_id})


class PlanView(
    discord.ui.View
):  # Create a class called MyView that subclasses discord.ui.View
    def __init__(self):
        super().__init__(timeout=None)  # timeout of the view must be set to None

    @discord.ui.button(
        label="Basic", row=0, style=discord.ButtonStyle.primary, custom_id="basic"
    )
    async def first_button_callback(self, button, interaction):
        embed = discord.Embed(
            title="Chosen Plan",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name=plans[0]["name"],
            value=(
                f"Price: ${plans[0]['price']}\n"
                f"Concurrent Streams: {plans[0]['concurrent_streams']}\n"
                f"Downloads Enabled: {'Yes' if plans[0]['downloads_enabled'] else 'No'}\n"
                f"4K Enabled: {'Yes' if plans[0]['4k_enabled'] else 'No'}\n"
            ),
            inline=False,
        )
        await interaction.response.send_message(
            view=PaymentOptionsView(plan=plans[0]), ephemeral=True, embed=embed
        )

    @discord.ui.button(
        label="Standard", row=0, style=discord.ButtonStyle.primary, custom_id="standard"
    )
    async def second_button_callback(self, button, interaction):
        embed = discord.Embed(
            title="Chosen Plan",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name=plans[1]["name"],
            value=(
                f"Price: ${plans[1]['price']}\n"
                f"Concurrent Streams: {plans[1]['concurrent_streams']}\n"
                f"Downloads Enabled: {'Yes' if plans[1]['downloads_enabled'] else 'No'}\n"
                f"4K Enabled: {'Yes' if plans[1]['4k_enabled'] else 'No'}\n"
            ),
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed, view=PaymentOptionsView(plan=plans[1]), ephemeral=True
        )

    @discord.ui.button(
        label="Extra", row=0, style=discord.ButtonStyle.primary, custom_id="extra"
    )
    async def third_button_callback(self, button, interaction):
        embed = discord.Embed(
            title="Chosen Plan",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name=plans[2]["name"],
            value=(
                f"Price: ${plans[2]['price']}\n"
                f"Concurrent Streams: {plans[2]['concurrent_streams']}\n"
                f"Downloads Enabled: {'Yes' if plans[2]['downloads_enabled'] else 'No'}\n"
                f"4K Enabled: {'Yes' if plans[2]['4k_enabled'] else 'No'}\n"
            ),
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed, view=PaymentOptionsView(plan=plans[2]), ephemeral=True
        )

    @discord.ui.button(
        label="Complete Payment",
        row=1,
        style=discord.ButtonStyle.green,
        custom_id="complete_payment",
    )
    async def fourth_button_callback(self, button, interaction):
        compelte = await complete_payment(interaction.user.id)
        await interaction.response.send_message(compelte, ephemeral=True)

    @discord.ui.button(
        label="Cancel Payment",
        row=1,
        style=discord.ButtonStyle.red,
        custom_id="cancel_payment",
    )
    async def fifth_button_callback(self, button, interaction):
        await interaction.response.send_message(
            await cancel_payment(interaction.user.id), ephemeral=True
        )


class PaymentOptionsView(discord.ui.View):
    def __init__(self, plan):
        super().__init__(timeout=None)
        self.plan = plan
        print(self.plan["onetime_stripe_price_id"])

    @discord.ui.button(
        label="One Time", row=0, style=discord.ButtonStyle.primary, custom_id="one-time"
    )
    async def first_button_callback(self, button, interaction):
        count = await db_plex["plex"].count_documents({})
        if count == 100:
            await interaction.response.send_message(
                "The server is currently full. Please try again later.", ephemeral=True
            )
            return
        check = await db_plex["plex"].find_one({"discord_id": interaction.user.id})
        if check != None:
            await interaction.response.send_message(
                "You are already in the database, please run /migrate. If this is in error, please contact an admin.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            EmailModal(
                title="One Time Payment",
                plan=self.plan["onetime_stripe_price_id"],
                plan_name=self.plan["name"],
            )
        )


@bot.slash_command(guild_ids=[GUILD_ID])
async def migrate(ctx):
    record = await db_plex["plex"].find_one({"discord_id": ctx.author.id})
    if record == None:
        await ctx.respond(
            "You are not a current paid user. Please use #join to subscribe.",
            ephemeral=True,
        )
        return
    else:
        selected_plan = next(
            (plan for plan in plans if plan["name"] == record["plan_name"]), None
        )
        downloads_enabled = selected_plan["downloads_enabled"]
        enabled_4k = selected_plan["4k_enabled"]
        if enabled_4k:
            add_sections = sections_all
        else:
            add_sections = sections_standard
        try:
            plex.myPlexAccount().inviteFriend(
                record["email"],
                plex,
                allowSync=downloads_enabled,
                sections=add_sections,
            )
        except Exception as e:
            await ctx.respond(
                f"There was an error migrating your account. Please contact an admin. Error: {e}",
                ephemeral=True,
            )
            return
        plan = record["plan_name"]
        # add role to user
        role_id = next(item for item in plans if item["name"] == plan)["role_id"]
        role = discord.utils.get(bot.get_guild(int(GUILD_ID)).roles, id=int(role_id))
        await bot.get_guild(int(GUILD_ID)).get_member(record["discord_id"]).add_roles(
            role
        )
        expiration_date = record["expiration_date"]
        days_remaining = (expiration_date - datetime.datetime.now()).days

        await ctx.respond(
            f"Your account has been migrated to {plan}, and expires in {days_remaining}. Please check your email for an invite to the new server.",
            ephemeral=True,
        )


@bot.slash_command(guild_ids=[GUILD_ID])
async def send_plan_menu(ctx):
    if int(DISCORD_ADMIN_ROLE_ID) not in [role.id for role in ctx.author.roles]:
        await ctx.respond(
            "You do not have permission to use this command.", ephemeral=True
        )
        return
    embed = discord.Embed(
        title="Plex Plans",
        description="Choose the plan that best suits your needs:",
        color=discord.Color.blue(),
    )
    for plan in plans:
        embed.add_field(
            name=plan["name"],
            value=(
                f"Price: ${plan['price']}\n"
                f"Concurrent Streams: {plan['concurrent_streams']}\n"
                f"Downloads Enabled: {'Yes*' if plan['downloads_enabled'] else 'No'}\n"
                f"4K Enabled: {'Yes' if plan['4k_enabled'] else 'No'}\n"
            ),
            inline=True,
        )

    embed.add_field(
        name="One Time Payment Plan",
        value="Your card details are not saved, you will need to manually add more time to avoid being removed.",
        inline=False,
    )

    embed.add_field(
        name="",
        value="*Plex accounts created after August 1, 2022 require a Plex Pass to utilize downloads. For more information, see https://support.plex.tv/articles/downloads-sync-faq/.",
        inline=False,
    )

    await ctx.send(embed=embed, view=PlanView())

    await ctx.respond(
        f"Sent the embed, persistent status: {PlanView.is_persistent(PlanView())}",
        ephemeral=True,
    )

@bot.slash_command(guild_ids=[GUILD_ID])
async def ping(ctx):
    # round latency to 2 decimal places
    await ctx.respond(f"Pong! ({round(bot.latency*1000, 2)}ms)", ephemeral=True)

async def cancel_payment(discord_id):
    try:
        # Check if the user has a pending payment
        existing_payment = await db_payments["payments"].find_one(
            {"discord_id": discord_id, "paid": False, "active": True}
        )
        if not existing_payment:
            return "No pending invoice found."

        invoice_id = existing_payment["invoice_id"]

        # Retrieve the invoice from Stripe
        invoice = await stripe.Invoice.retrieve(invoice_id)

        # Check if the invoice is already paid
        if invoice.status == "paid":
            return "The invoice has already been paid. If you want a refund, please contact The Governor. Please use the complete button to complete the process."

        # Cancel the invoice
        await stripe.Invoice.void_invoice(invoice_id)

        # Remove the invoice from the "payments" MongoDB collection
        await db_payments["payments"].delete_one(
            {"discord_id": discord_id, "invoice_id": invoice_id}
        )
        return "The invoice has been cancelled."

    except Exception as e:
        return f"Error cancelling your invoice, {e}"


async def contactAdmin(message):
    admin = bot.get_user(int(DISCORD_ADMIN_ID))
    if admin is not None:
        try:
            await admin.send(message)
        except discord.Forbidden:
            print("The bot does not have permission to send messages to the admin.")
    else:
        print("Admin not found.")


async def complete_payment(discord_id):
    try:
        payment_data = await db_payments["payments"].find_one(
            {"discord_id": discord_id, "paid": False, "active": True}
        )
        if not payment_data:
            return "No pending payment found."
        invoice_id = payment_data["invoice_id"]
        invoice = await stripe.Invoice.retrieve(invoice_id)

        if invoice.status != "paid":
            return "The invoice has not been paid yet."

        # Update payment status in the database
        await db_payments["payments"].update_one(
            {"discord_id": discord_id, "invoice_id": invoice_id},
            {"$set": {"paid": True, "active": False}},
        )
        # Add user to Plex
        user_email = payment_data["email"]
        plex_test = await db_plex["plex"].find_one({"email": user_email})
        # edit the expiry date in the plex database
        if plex_test:  # if user already exists
            current_expiry = plex_test["expiration_date"]
            expiration_date = current_expiry + datetime.timedelta(days=30)
            await db_plex["plex"].update_one(
                {"email": user_email},
                {
                    "$set": {
                        "expiration_date": expiration_date,
                        "sent_notifications": [],
                    }
                },
            )

            return "Time was added to your account."
        try:
            add_to_plex_result = await add_to_plex(
                user_email, discord_id, payment_data["plan_name"]
            )
            if add_to_plex_result != True:
                return f"Payment verified, but there was an error adding you to Plex. Please contact an administrator. Error: {add_to_plex_result}"
        except Exception as e:
            return f"Payment verified, but there was an error adding you to Plex. Please contact an administrator. Error: {e}"
        expiration_date = datetime.datetime.utcnow() + datetime.timedelta(days=30)
        await db_plex["plex"].update_one(
            {"email": user_email},
            {
                "$set": {
                    "expiration_date": expiration_date,
                    "plan_id": payment_data["plan_id"],
                    "plan_name": payment_data["plan_name"],
                    "sent_notifications": [],
                }
            },
        )
        plan = payment_data["plan_name"]
        # find the role id in plans list from the plan name
        role_id = next(item for item in plans if item["name"] == plan)["role_id"]
        role = discord.utils.get(bot.get_guild(int(GUILD_ID)).roles, id=int(role_id))
        await bot.get_guild(int(GUILD_ID)).get_member(discord_id).add_roles(role)
        return "Payment verified! You have been added to Plex."
    except Exception as e:
        return f"Error, please contact an administrator. Error: {e}"


async def isExpired(date):
    expired = date < datetime.datetime.utcnow()
    remaining = date - datetime.datetime.utcnow()
    return expired, math.ceil(remaining.total_seconds() / 86400)


@tasks.loop(hours=12)
async def subscriptionCheckerLoop():
    print("Running subscription checker loop...")
    expired_removed = 0
    await contactAdmin("Starting subscription checker loop...")
    users = await db_plex["plex"].find().to_list(length=None)
    for user in users:
        expired_check = await isExpired(user["expiration_date"])
        expired = expired_check[0]
        remaining = expired_check[1]

        if expired:
            await contactAdmin(f'{user["discord_id"]}\'s subscription has expired.')
            email = user["email"]
            plan = user["plan_name"]
            discord_id = user["discord_id"]
            await db_plex["plex"].delete_one({"discord_id": user["discord_id"]})
            expired_removed += 1
            try:
                plex.myPlexAccount().removeFriend(email)
            except:
                try:
                    plex.myPlexAccount().cancelInvite(email)
                except:
                    pass
            # give user the role according to their plan

            # find the role id in plans list from the plan name
            role_id = next(item for item in plans if item["name"] == plan)["role_id"]
            role = discord.utils.get(
                bot.get_guild(int(GUILD_ID)).roles, id=int(role_id)
            )
            # remove role
            try:
                await bot.get_guild(int(GUILD_ID)).get_member(
                    int(user["discord_id"])
                ).remove_roles(role)
            except:
                await contactAdmin(
                    f'Failed to remove role from {user["discord_id"]}. User left server?'
                )
            # message user
            try:
                await bot.get_guild(int(GUILD_ID)).get_member(int(discord_id)).send(
                    f"Your subscription has expired. You have been removed from the server."
                )
            except:
                await contactAdmin(
                    f'Failed to message {user["discord_id"]}. User left server?'
                )
        else:
            days_remaining = remaining
            if (
                days_remaining in [5, 3, 1]
                and days_remaining not in user["sent_notifications"]
            ):
                # add the days_remaining to the list sent_notifications
                await db_plex["plex"].update_one(
                    {"discord_id": user["discord_id"]},
                    {"$push": {"sent_notifications": days_remaining}},
                )
                try:
                    await bot.get_guild(int(GUILD_ID)).get_member(
                        int(user["discord_id"])
                    ).send(
                        f"Your subscription will expire in {days_remaining} days. Please renew it to avoid being removed."
                    )
                except:
                    await contactAdmin(
                        f'Failed to message {user["discord_id"]}. User left server?'
                    )
    await contactAdmin(
        f"Subscription checker loop completed, sleeping for 12 hours. Removed {expired_removed} expired users."
    )
    return


@tasks.loop(hours=12)
async def stats_update():
    try:
        movie_count = 0
        tv_count = 0
        episodes_count = 0
        for movie_section in sections_movies:
            movie_count += movie_section.totalSize

        for tv_section in sections_tv:
            tv_count += tv_section.totalSize
            episodes_count += tv_section.totalViewSize(libtype="episode")

        # add a comma to each count if needed
        movie_count = "{:,}".format(movie_count)
        tv_count = "{:,}".format(tv_count)
        episodes_count = "{:,}".format(episodes_count)

        # find the discord category with the STATS_CHANNEL_ID value
        stats_category = discord.utils.get(
            bot.get_guild(int(GUILD_ID)).categories, id=int(STATS_CHANNEL_ID)
        )
        # check how many voice channels in this category
        voice_channels = stats_category.voice_channels

        if voice_channels == 0:
            # create two channels, one for movies and one for tv shows and episodes counts
            mc = await stats_category.create_voice_channel(name="Movies")
            tc = await stats_category.create_voice_channel(name="TV Shows")
            ec = await stats_category.create_voice_channel(name="Episodes")
        else:
            # get the channels
            for channel in voice_channels:
                await channel.delete()
            mc = await stats_category.create_voice_channel(name="Movies")
            tc = await stats_category.create_voice_channel(name="TV Shows")
            ec = await stats_category.create_voice_channel(name="Episodes")
        # make each voice channel not joinable
        await mc.set_permissions(
            bot.get_guild(int(GUILD_ID)).default_role, connect=False
        )
        await tc.set_permissions(
            bot.get_guild(int(GUILD_ID)).default_role, connect=False
        )
        await ec.set_permissions(
            bot.get_guild(int(GUILD_ID)).default_role, connect=False
        )
        # update the MC (movies) and TC (others), {count} Movies / {count} Shows | {count} Episodes
        await mc.edit(name=f"{movie_count} Movies")
        await tc.edit(name=f"{tv_count} Shows")
        await ec.edit(name=f"{episodes_count} Episodes")
        await contactAdmin(
            f"Stats updated. Movies: {movie_count}, TV Shows: {tv_count}, Episodes: {episodes_count}"
        )
    except Exception as e:
        await contactAdmin(f"Error updating stats: {e}")


bot.run(DISCORD_TOKEN)
