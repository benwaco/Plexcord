import os

import discord
import dotenv
import motor.motor_asyncio
import stripe
import yaml
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


def load_plans():
    with open("plans.yml", "r") as file:
        plans_data = yaml.safe_load(file)
    return plans_data["plans"]


plans = load_plans()


# Setup Stripe
stripe.api_key = STRIPE_API_KEY


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
    bot.add_view(PlanView())  # Registers a View for persistent listening
    print(f"We have logged in as {bot.user}")


async def add_to_plex(email, discord_id):
    try:
        plex.myPlexAccount().inviteFriend(
            email,
            plex,
            allowSync=True,
            allowCameraUpload=True,
            filterMovies=None,
            filterTelevision=None,
            filterMusic=None,
            allowChannels=None,
        )
        # If successful, add the email, discord id and share status to the database
        await db_plex["plex"].insert_one(
            {
                "email": email,
                "discord_id": discord_id,
                "share_status": "pending",
                "archived": False,
            }
        )
        return True
    except Exception as e:
        print(e)
        return False


# Setup Slash Commands


# User
@bot.slash_command(guild_ids=[GUILD_ID])
async def ping(ctx):
    await ctx.respond(f"Pong! {int(bot.latency * 1000)}ms", ephemeral=True)



async def donate(email: str, stripe_price_id: str, discord_author_id: str):
    try:
        # Check if the user already has a pending invoice
        existing_payment = await db_payments["payments"].find_one(
            {"discord_id": discord_author_id, "paid": False}
        )
        if existing_payment:
            return (
                "You already have a pending invoice. Please use `/cancel_invoice` to cancel the existing invoice before creating a new one.",
            )

        # Create Stripe customer
        customer = stripe.Customer.create(email=email)

        # Create Stripe invoice
        invoice_item = stripe.InvoiceItem.create(
            customer=customer.id,
            price=stripe_price_id,  # Replace with your Stripe price ID
        )

        invoice = stripe.Invoice.create(
            customer=customer.id,
            auto_advance=True,
            pending_invoice_items_behavior="include",
        )

        # Finalize the invoice
        finalised_invoice = stripe.Invoice.finalize_invoice(invoice.id)

        # Log unpaid invoice to the "payments" MongoDB collection
        await db_payments["payments"].insert_one(
            {
                "discord_id": discord_author_id,
                "email": email,
                "invoice_id": finalised_invoice.id,
                "paid": False,
                "invoice_url": finalised_invoice.hosted_invoice_url,
            }
        )

        # Send the invoice URL to the user
        # Direct message the user the link as well
        return finalised_invoice.hosted_invoice_url
    except Exception as e:
        print(e)
        return f"Error creating your invoice, {e}"


@bot.slash_command(guild_ids=[GUILD_ID])
async def status(ctx):
    try:
        user_data_list = (
            await db_plex["plex"]
            .find({"discord_id": ctx.author.id})
            .to_list(length=None)
        )
        if not user_data_list:
            await ctx.respond(f"You are not in the database.", ephemeral=True)
            return

        unarchived_data = None
        for user_data in user_data_list:
            if not user_data["archived"]:
                unarchived_data = user_data
                break

        if unarchived_data is None:
            email = user_data_list[0]["email"]
            share_status = user_data_list[0]["share_status"]
            await ctx.respond(
                f"Your email is {email}, and your share status is {share_status}. You are archived. This means that you have been removed from the server manually or we have removed you due to non-payment. If you wish to be re-added, please contact us.",
                ephemeral=True,
            )
        else:
            email = unarchived_data["email"]
            share_status = unarchived_data["share_status"]
            await ctx.respond(
                f"Your email is {email}, and your share status is {share_status}.",
                ephemeral=True,
            )
    except Exception as e:
        print(e)
        await ctx.respond(f"Error retrieving your share status, {e}", ephemeral=True)


class BasicModal(discord.ui.Modal):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.add_item(discord.ui.InputText(label="Email Address"))
    async def callback(self, interaction: discord.Interaction):
        email = self.children[0].value
        print(email, interaction.user.id, plans[0]['stripe_price_id'])
        donate_return = await donate(email, plans[0]['stripe_price_id'], interaction.user.id)
        await interaction.response.send_message(f"Please pay the invoice at the following URL: {donate_return}, the link has also been messaged to you.", ephemeral=True)
        await interaction.user.send(f"Please pay the invoice at the following URL: {donate_return}.")

class TestFormModal(discord.ui.Modal):
    def __init__(self, plan, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.add_item(discord.ui.InputText(label="Email Address"))
        self.plan = plan

    async def callback(self, interaction: discord.Interaction):
        email = self.children[0].value
        donate_return = await donate(email, self.plan, interaction.user.id)
        await interaction.response.send_message(f"Please pay the invoice at the following URL: {donate_return}, the link has also been messaged to you.", ephemeral=True)
        await interaction.user.send(f"Please pay the invoice at the following URL: {donate_return}.")

class StandardModal(discord.ui.Modal):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.add_item(discord.ui.InputText(label="Email Address"))

    async def callback(self, interaction: discord.Interaction):
        email = self.children[0].value
        print(email, interaction.user.id, plans[1]['stripe_price_id'])
        donate_return = await donate(email, plans[1]['stripe_price_id'], interaction.user.id)
        await interaction.response.send_message(donate_return, ephemeral=True)

class ExtraModal(discord.ui.Modal):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.add_item(discord.ui.InputText(label="Email Address"))

    async def callback(self, interaction: discord.Interaction):
        email = self.children[0].value
        print(email, interaction.user.id, plans[2]['stripe_price_id'])
        donate_return = await donate(email, plans[2]['stripe_price_id'], interaction.user.id)
        await interaction.response.send_message(donate_return, ephemeral=True)

class PlanView(
    discord.ui.View
):  # Create a class called MyView that subclasses discord.ui.View
    def __init__(self):
        super().__init__(timeout=None)  # timeout of the view must be set to None

    @discord.ui.button(
        label="Basic", row=0, style=discord.ButtonStyle.primary, custom_id="basic"
    )
    async def first_button_callback(self, button, interaction):
        await interaction.response.send_message(view=PaymentOptionsView(plan=plans[0]))

    @discord.ui.button(
        label="Standard", row=0, style=discord.ButtonStyle.primary, custom_id="standard"
    )
    async def second_button_callback(self, button, interaction):
        await interaction.response.send_message(view=PaymentOptionsView(plan=plans[1]), ephemeral=True)

    @discord.ui.button(
        label="Extra", row=0, style=discord.ButtonStyle.primary, custom_id="extra"
    )
    async def third_button_callback(self, button, interaction):
        await interaction.response.send_message(view=PaymentOptionsView(plan=plans[2]), ephemeral=True)

class PaymentOptionsView(
    discord.ui.View
):
    def __init__(self, plan):
        super().__init__(timeout=None) 
        self.plan = plan
        print(self.plan['onetime_stripe_price_id'])

    @discord.ui.button(
        label="One Time", row=0, style=discord.ButtonStyle.primary, custom_id="one-time"
    )
    async def first_button_callback(self, button, interaction):
        await interaction.response.send_modal(TestFormModal(title='One Time Payment', plan=self.plan['onetime_stripe_price_id']))

    @discord.ui.button(
        label="Recurring", row=0, style=discord.ButtonStyle.primary, custom_id="recurring"
    )
    async def second_button_callback(self, button, interaction):
        await interaction.response.send_modal(TestFormModal(title='Recurring Payment', plan=self.plan['subscription_stripe_price_id']))


@bot.slash_command(guild_ids=[GUILD_ID])
async def send_plan_menu(ctx):
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
                f"Downloads Enabled: {'Yes' if plan['downloads_enabled'] else 'No'}\n"
                f"4K Enabled: {'Yes' if plan['4k_enabled'] else 'No'}\n"
            ),
            inline=False,
        )
    
    embed.add_field(
        name="One Time Payment Plan",
        value="Your card details are not saved, you will need to manually add more time to avoid being removed.",
        inline=False,
    )

    embed.add_field(
        name="Recurring Payment Plan",
        value="Your card details are saved, and you will be charged automatically every month. Please use the /cancel command to cancel your subscription or contact support. You will be automatically removed if your card is declined or you cancel.",
        inline=False,
    )

    await ctx.send(embed=embed, view=PlanView())

    await ctx.respond(
        f"Sent the embed, persistent status: {PlanView.is_persistent(PlanView())}",
        ephemeral=True,
    )



@bot.slash_command(guild_ids=[GUILD_ID])
async def cancel_invoice(ctx):
    try:
        # Check if the user has a pending invoice
        existing_payment = await db_payments["payments"].find_one(
            {"discord_id": ctx.author.id, "paid": False}
        )
        if not existing_payment:
            await ctx.respond("No pending invoice found.", ephemeral=True)
            return

        invoice_id = existing_payment["invoice_id"]

        # Retrieve the invoice from Stripe
        invoice = stripe.Invoice.retrieve(invoice_id)

        # Check if the invoice is already paid
        if invoice.status == "paid":
            await ctx.respond(
                "The invoice has already been paid. If you want a refund, please contact an administrator.",
                ephemeral=True,
            )
            return

        # Cancel the invoice
        stripe.Invoice.delete(invoice_id)

        # Remove the invoice from the "payments" MongoDB collection
        await db_payments["payments"].delete_one(
            {"discord_id": ctx.author.id, "invoice_id": invoice_id}
        )

        await ctx.respond("The invoice has been cancelled.", ephemeral=True)
    except Exception as e:
        print(e)
        await ctx.respond(f"Error cancelling your invoice, {e}", ephemeral=True)


@bot.slash_command(guild_ids=[GUILD_ID])
async def payment_complete(ctx):
    try:
        payment_data = await db_payments["payments"].find_one(
            {"discord_id": ctx.author.id, "paid": False}
        )
        if not payment_data:
            await ctx.respond("No pending payment found.", ephemeral=True)
            return

        invoice_id = payment_data["invoice_id"]

        # Verify payment status with Stripe
        invoice = stripe.Invoice.retrieve(invoice_id)

        if invoice.status == "paid":
            # Update payment status in the database
            await db_payments["payments"].update_one(
                {"discord_id": ctx.author.id, "invoice_id": invoice_id},
                {"$set": {"paid": True}},
            )

            # Add user to Plex
            user_email = payment_data["email"]
            await add_to_plex(user_email, ctx.author.id)

            await ctx.respond(
                "Payment verified! You have been added to Plex.", ephemeral=True
            )
        else:
            await ctx.respond(
                "Your payment hasn't been verified yet. Please wait for a while and try again.",
                ephemeral=True,
            )

    except Exception as e:
        print(e)
        await ctx.respond(f"Error verifying your payment, {e}", ephemeral=True)


# Admin


@bot.slash_command(guild_ids=[GUILD_ID])
async def add(ctx, email: str):
    if DISCORD_ADMIN_ROLE_ID not in [role.id for role in ctx.author.roles]:
        await ctx.respond(
            "You do not have permission to use this command.", ephemeral=True
        )
        return

    success = await add_to_plex(email, ctx.author.id)
    if success:
        await ctx.respond(f"Invited {email} to plex server", ephemeral=True)
    else:
        await ctx.respond(f"Error adding {email} to plex server", ephemeral=True)


# make a slash command to remove a user from plex (based on provided discord id) and change the share status to 'manually removed' in the database
@bot.slash_command(guild_ids=[GUILD_ID])
async def remove(ctx, discord_id: str):
    if DISCORD_ADMIN_ROLE_ID not in [role.id for role in ctx.author.roles]:
        await ctx.respond(
            "You do not have permission to use this command.", ephemeral=True
        )
        return
    discord_id = int(discord_id)
    try:
        # user_data = await db_plex["plex"].find_one({"discord_id": discord_id}) change to check if it is not archived
        user_data = await db_plex["plex"].find_one(
            {"discord_id": discord_id, "archived": False}
        )

        if user_data is None:
            await ctx.respond(
                f"User with Discord ID {discord_id} not found in the database or user is archived.",
                ephemeral=True,
            )
            return

        email = user_data["email"]
        print(email)
        # remove the user from plex
        plex.myPlexAccount().removeFriend(email)
        # update the database
        await db_plex["plex"].update_one(
            {"discord_id": discord_id}, {"$set": {"share_status": "removed manually"}}
        )
        await db_plex["plex"].update_one(
            {"discord_id": discord_id}, {"$set": {"archived": True}}
        )
        await ctx.respond(f"Removed {email} from plex server", ephemeral=True)
    except Exception as e:
        print(e)
        await ctx.respond(f"Error removing user from plex server, {e}", ephemeral=True)


# make a basic slash command for customers to view their current share status, print their plex email and share status from the database. tell user if they are archived and explain what archived means


@bot.slash_command(guild_ids=[GUILD_ID])
async def lookup(ctx, discord_id: str):
    if DISCORD_ADMIN_ROLE_ID not in [role.id for role in ctx.author.roles]:
        await ctx.respond(
            "You do not have permission to use this command.", ephemeral=True
        )
        return
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
            await ctx.respond(
                f"{discord_id}'s email is {email}, and their share status is {share_status}. They are archived. This means that they have been removed from the server manually or we have removed them due to non-payment. If they wish to be re-added, they should contact us.",
                ephemeral=True,
            )
        else:
            await ctx.respond(
                f"{discord_id}'s email is {email}, and their share status is {share_status}.",
                ephemeral=True,
            )
    except Exception as e:
        print(e)
        await ctx.respond(
            f"Error retrieving {discord_id}'s share status, {e}", ephemeral=True
        )


@bot.slash_command(guild_ids=[GUILD_ID])
async def count(ctx):
    if DISCORD_ADMIN_ROLE_ID not in [role.id for role in ctx.author.roles]:
        await ctx.respond(
            "You do not have permission to use this command.", ephemeral=True
        )
        return
    await ctx.respond(f"{len(account.users())}/100 users", ephemeral=True)


@tasks.loop(minutes=5.0)
async def update_status_loop():
    print("Starting update...")

    # Fetch pending users from the database
    pending_users = (
        await db_plex["plex"]
        .find({"share_status": "pending", "archived": False})
        .to_list(length=None)
    )
    accepted_users = (
        await db_plex["plex"]
        .find({"share_status": "accepted", "archived": False})
        .to_list(length=None)
    )

    # Retrieve the list of friends and pending invites from the Plex server
    plex_friends = plex.myPlexAccount().users()
    plex_pending_invites = plex.myPlexAccount().pendingInvites(
        includeSent=True, includeReceived=False
    )

    # Handle pending users
    for user in pending_users:
        # Check if the user accepted the invitation or is already a friend
        plex_user = next(
            (friend for friend in plex_friends if friend.email == user["email"]), None
        )

        if plex_user:
            # Update the share status in the database
            await db_plex["plex"].update_one(
                {"email": user["email"]}, {"$set": {"share_status": "accepted"}}
            )
            print(f'Updated share status for {user["email"]} to "accepted"')
        else:
            # Check if the user still has a pending invitation
            pending_invite = next(
                (
                    invite
                    for invite in plex_pending_invites
                    if invite.email == user["email"]
                ),
                None,
            )

            if not pending_invite:
                # Update the share status in the database
                await db_plex["plex"].update_one(
                    {"email": user["email"]}, {"$set": {"share_status": "rejected"}}
                )
                print(f'Updated share status for {user["email"]} to "rejected"')

    # Handle accepted users
    for user in accepted_users:
        # Check if the user is still a friend on the Plex server
        plex_user = next(
            (friend for friend in plex_friends if friend.email == user["email"]), None
        )

        if not plex_user:
            # Update the share status in the database
            await db_plex["plex"].update_one(
                {"email": user["email"]}, {"$set": {"share_status": "left plex"}}
            )
            print(f'Updated share status for {user["email"]} to "left plex"')

    print("Update completed.")


update_status_loop.start()

bot.run(DISCORD_TOKEN)
