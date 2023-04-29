import datetime
import os

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
db_subscriptions = client["pycord"]

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
                "share_status": "active",
                "subscription_status": "active",
                "expiration_date": None,
                "plan_id": None,
                "plan_name": None,
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


async def donate(
    email: str, stripe_price_id: str, discord_author_id: str, plan_name
):
    try:
        # Check if the user already has a pending invoice
        existing_payment = await db_payments["payments"].find_one(
            {"discord_id": discord_author_id, "active": True}
        )
        if existing_payment is not None:
            return (
                f"You already have a pending invoice. Please pay it at {existing_payment['invoice_url']} or use the cancel button to cancel the existing invoice before creating a new one.",
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
        # elif type == "recurring":
        #     session = await stripe.checkout.Session.create(
        #         customer=customer.id,
        #         line_items=[
        #             {
        #                 "price": stripe_price_id,
        #                 "quantity": 1,
        #             },
        #         ],
        #         mode="subscription",
        #         success_url="https://pastebin.com/raw/hNTkhSb8",
        #     )
        #     await db_payments["payments"].insert_one(
        #         {
        #             "discord_id": discord_author_id,
        #             "email": email,
        #             "session_id": session.id,
        #             "paid": False,
        #             "invoice_url": session.url,
        #             "type": "recurring",
        #         }
        #     )
        #     return session.url
    except Exception as e:
        print(e)
        return f"Error creating your subscription, {e}"


async def add_time(discord_id):
    plex_data = await db_plex["plex"].find_one({"discord_id": discord_id})
    email, stripe_price_id, plan_name = (
        plex_data["email"],
        plex_data["plan_id"],
        plex_data["plan_name"],
    )

    return await donate(email, stripe_price_id, discord_id, plan_name)


# async def donate(
#     email: str, stripe_price_id: str, discord_author_id: str, plan_name, type
# ):


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
        print(donate_return)
        if donate_return[0].startswith("You"):
            print("TRUUUE")
            await interaction.response.send_message(
                donate_return[0],
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Please pay the invoice at the following URL: {donate_return}, the link has also been messaged to you.",
            ephemeral=True,
        )
        await interaction.user.send(
            f"Please pay the invoice at the following URL: {donate_return}."
        )


@bot.slash_command(guild_ids=[GUILD_ID])
async def send_subscription_menu(ctx):
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
        print(subscription_info)
        expiration_date = subscription_info[0]
        plan_name = subscription_info[1]
        embed = discord.Embed(
            title="Manage Subscription",
            description="Use the buttons below to modify your subscription.",
        )
        print(plan_name)
        print(expiration_date)
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
            print("TRUUUE")
            await interaction.response.send_message(
                donate_return[0],
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Please pay the invoice at the following URL: {donate_return}, the link has also been messaged to you.",
            ephemeral=True,
        )
        await interaction.user.send(
            f"Please pay the invoice at the following URL: {donate_return}."
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
        print(compelte)
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
        await interaction.response.send_modal(
            EmailModal(
                title="One Time Payment",
                plan=self.plan["onetime_stripe_price_id"],
                plan_name=self.plan["name"],
            )
        )

    # @discord.ui.button(
    #     label="Recurring",
    #     row=0,
    #     style=discord.ButtonStyle.primary,
    #     custom_id="recurring",
    # )
    # async def second_button_callback(self, button, interaction):
    #     await interaction.response.send_modal(
    #         EmailModal(
    #             title="Recurring Payment",
    #             plan=self.plan["subscription_stripe_price_id"],
    #             type="recurring",
    #         )
    #     )


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

    # embed.add_field(
    #     name="Recurring Payment Plan",
    #     value="Your card details are saved, and you will be charged automatically every month. Please use the /cancel command to cancel your subscription or contact support. You will be automatically removed if your card is declined or you cancel.",
    #     inline=False,
    # )

    await ctx.send(embed=embed, view=PlanView())

    await ctx.respond(
        f"Sent the embed, persistent status: {PlanView.is_persistent(PlanView())}",
        ephemeral=True,
    )


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
            return "The invoice has already been paid. If you want a refund, please contact an administrator. Please run /payment_complete to complete the process."

        # Cancel the invoice
        await stripe.Invoice.void_invoice(invoice_id)

        # Remove the invoice from the "payments" MongoDB collection
        await db_payments["payments"].delete_one(
            {"discord_id": discord_id, "invoice_id": invoice_id}
        )
        # change the active status in payments to false
        await db_payments["payments"].update_one(
            {"discord_id": discord_id, "invoice_id": invoice_id},
            {"$set": {"active": False}},
        )
        return "The invoice has been cancelled."

        # else:
        #     session_id = existing_payment["session_id"]
        #     # check if already paid
        #     session = stripe.checkout.Session.retrieve(session_id)
        #     if session.payment_status == "paid":
        #         await ctx.respond(
        #             "The subscription has already been paid. If you want a refund, please contact an administrator. Please run /payment_complete to complete the process.",
        #             ephemeral=True,
        #         )
        #         return
        #     stripe.checkout.Session.expire(session_id)
        #     await db_payments["payments"].delete_one(
        #         {"discord_id": ctx.author.id, "invoice_id": invoice_id}
        #     )
        #     await ctx.respond("The subscription has been cancelled.", ephemeral=True)
    except Exception as e:
        print(e)
        return f"Error cancelling your invoice, {e}"


async def contactAdmin(message):
    admin = bot.get_user(int(DISCORD_ADMIN_ID))
    if admin is not None:
        try:
            await admin.send(message)
            print(f"Message sent to admin: {message}")
        except discord.Forbidden:
            print("The bot does not have permission to send messages to the admin.")
    else:
        print("Admin not found.")


async def complete_payment(discord_id):
    print("payment complete")
    try:
        payment_data = await db_payments["payments"].find_one(
            {"discord_id": discord_id, "paid": False, "active": True}
        )
        print(payment_data)
        if not payment_data:
            return "No pending payment found."
        invoice_id = payment_data["invoice_id"]
        invoice = await stripe.Invoice.retrieve(invoice_id)
        if invoice.status == "paid":
            # Update payment status in the database
            await db_payments["payments"].update_one(
                {"discord_id": discord_id, "invoice_id": invoice_id},
                {"$set": {"paid": True, "active": False}},
            )
            # Add user to Plex
            user_email = payment_data["email"]
            # edit the expiry date in the plex database
            try:
                if await db_plex["plex"].find_one({"email": user_email}):
                    current_expiry = await db_plex["plex"].find_one({"email": user_email})   
                    current_expiry = current_expiry['expiration_date']  
                    expiration_date = current_expiry + datetime.timedelta(days=30)  
                    await db_plex["plex"].update_one(
                        {"email": user_email},
                        {
                            "$set": {
                                "expiration_date": expiration_date,
                            }
                        },
                    )

                    return "Time was added to your account."
                await add_to_plex(user_email, discord_id)
                #check if user already has an expiration date (adding time) 
                current_expiry = await db_plex["plex"].find_one({"email": user_email})['expiration_date']         
                expiration_date = datetime.datetime.now() + datetime.timedelta(days=30)
                await db_plex["plex"].update_one(
                    {"email": user_email},
                    {
                        "$set": {
                            "expiration_date": expiration_date,
                            "plan_id": payment_data["plan_id"],
                            "plan_name": payment_data["plan_name"],
                        }
                    },
                )
                # give user the role according to their plan
                plan = payment_data["plan_name"]
                # find the role id in plans list from the plan name
                role_id = next(item for item in plans if item["name"] == plan)[
                    "role_id"
                ]
                role = discord.utils.get(
                    bot.get_guild(int(GUILD_ID)).roles, id=int(role_id)
                )
                await bot.get_guild(int(GUILD_ID)).get_member(discord_id).add_roles(
                    role
                )
                return "Payment verified! You have been added to Plex."
            except Exception as e:
                # give user the role according to their plan
                plan = payment_data["plan_name"]
                # find the role id in plans list from the plan name
                role_id = next(item for item in plans if item["name"] == plan)[
                    "role_id"
                ]
                role = discord.utils.get(
                    bot.get_guild(int(GUILD_ID)).roles, id=int(role_id)
                )
                await bot.get_guild(int(GUILD_ID)).get_member(discord_id).add_roles(
                    role
                )

                return f"Payment verified, but there was an error adding you to Plex. Please contact an administrator. Error: {e}"
        else:
            return "Invoice not paid."
        # try:
        #     invoice_id = payment_data["invoice_id"]
        #     invoice = await stripe.Invoice.retrieve(invoice_id)
        #     print('3')
        # # except:
        #     session_id = payment_data["session_id"]
        #     session = await stripe.checkout.Session.retrieve(session_id)
        #     invoice = await stripe.Invoice.retrieve(session.subscription)

        # Verify payment status with Stripe
        # if payment_data["type"] == "onetime":
        #     print(invoice)
        #     print('test')
        #     if invoice.status == "paid":
        #         try:
        #             # Update payment status in the database

        #             # Add user to Plex
        #             user_email = payment_data["email"]
        #             print('test 2')
        #             # edit the expiry date in the plex database
        #             try:
        #                 await add_to_plex(user_email, discord_id)
        #                 await db_payments["payments"].update_one(
        #                     {"discord_id": discord_id, "invoice_id": invoice_id},
        #                     {"$set": {"paid": True}},
        #                 )
        #                 if plex_data["expiration_date"] == None:
        #                     expiration_date = (
        #                         datetime.datetime.now() + datetime.timedelta(days=30)
        #                     )
        #                     await db_plex["plex"].update_one(
        #                         {"email": user_email},
        #                         {"$set": {"expiration_date": expiration_date}},
        #                     )

        #                 else:
        #                     expiration_date = plex_data[
        #                         "expiration_date"
        #                     ] + datetime.timedelta(days=30)
        #                     await db_plex["plex"].update_one(
        #                         {"email": user_email},
        #                         {"$set": {"expiration_date": expiration_date}},
        #                     )
        #                     return "Payment verified! You have been added to Plex."

        #             except Exception as e:
        #                 print(e)
        #                 await contactAdmin(f"Error adding user to Plex: {e}")
        #                 return f"Payment verified, but there was an error adding you to Plex. Please contact an administrator. Error: {e}"

        #         except Exception as e:
        #             # problem adding user to plex
        #             return (
        #                 f"Payment verified, but there was an error adding you to Plex. Please contact an administrator. Error: {e}",
        #             )
        #     else:
        #         return (
        #             "Your payment hasn't been verified yet, if you paid already try again then contact an administrator if this error persists.",
        #         )
        # elif payment_data["type"] == "recurring":
        #     session_id = payment_data["session_id"]
        #     checkout = await stripe.checkout.Session.retrieve(session_id)
        #     if checkout.payment_status == "paid":
        #         # Update payment status in the database
        #         await db_payments["payments"].update_one(
        #             {"discord_id": discord_id, "session_id": session_id},
        #             {"$set": {"paid": True}},
        #         )

        #         # Add user to Plex
        #         user_email = payment_data["email"]
        #         await add_to_plex(user_email, discord_id)

        #         return "Payment verified! You have been added to Plex."

        #     else:
        #         return (
        #             "Your payment hasn't been verified yet. Please wait for a while and try again.",
        #         )

    except Exception as e:
        return f"Error verifying your payment, {e}"


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


# @tasks.loop(minutes=5)
# async def update_status_loop():
#     print("Starting update...")

#     # Fetch pending users from the database
#     pending_users = (
#         await db_plex["plex"]
#         .find({"share_status": "pending", "archived": False})
#         .to_list(length=None)
#     )
#     accepted_users = (
#         await db_plex["plex"]
#         .find({"share_status": "accepted", "archived": False})
#         .to_list(length=None)
#     )

#     # Retrieve the list of friends and pending invites from the Plex server
#     plex_friends = plex.myPlexAccount().users()
#     plex_pending_invites = plex.myPlexAccount().pendingInvites(
#         includeSent=True, includeReceived=False
#     )

#     # Handle pending users
#     for user in pending_users:
#         # Check if the user accepted the invitation or is already a friend
#         plex_user = next(
#             (friend for friend in plex_friends if friend.email == user["email"]), None
#         )

#         if plex_user:
#             # Update the share status in the database
#             await db_plex["plex"].update_one(
#                 {"email": user["email"]}, {"$set": {"share_status": "accepted"}}
#             )
#             print(f'Updated share status for {user["email"]} to "accepted"')
#         else:
#             # Check if the user still has a pending invitation
#             pending_invite = next(
#                 (
#                     invite
#                     for invite in plex_pending_invites
#                     if invite.email == user["email"]
#                 ),
#                 None,
#             )

#             if not pending_invite:
#                 # Update the share status in the database
#                 await db_plex["plex"].update_one(
#                     {"email": user["email"]}, {"$set": {"share_status": "rejected"}}
#                 )
#                 print(f'Updated share status for {user["email"]} to "rejected"')

#     # Handle accepted users
#     for user in accepted_users:
#         # Check if the user is still a friend on the Plex server
#         plex_user = next(
#             (friend for friend in plex_friends if friend.email == user["email"]), None
#         )

#         if not plex_user:
#             # Update the share status in the database
#             await db_plex["plex"].update_one(
#                 {"email": user["email"]}, {"$set": {"share_status": "left plex"}}
#             )
#             print(f'Updated share status for {user["email"]} to "left plex"')

#     print("Update completed.")


# @tasks.loop(minutes=1)
# async def update_subscription_loop():
#     # check every record in the subscription database
#     print("Starting subscription update...")
#     async for user in db_plex["plex"].find():
#         print("THE USER IS " + user)
#         if user["expired"] == True:
#             return
#         else:
#             # check if users subscription expired by looking up their discord id in the subscriptions database
#             discord_id = user["discord_id"]
#             # check if subscription['expiration_date'] is less than today's date in UTC
#             if user["expiration_date"] < datetime.datetime.utcnow():
#                 try:
#                     plex.MyPlexAccount.removeFriend(user["email"])
#                     await db_plex["plex"].update_one(
#                         {"discord_id": discord_id},
#                         {
#                             "$set": {"share_status": "inactive"},
#                             "$set": {"expired": True},
#                         },
#                     )

#                 except:
#                     try:
#                         plex.MyPlexAccount.cancelInvite(user["email"])
#                         await db_plex["plex"].update_one(
#                             {"discord_id": discord_id},
#                             {
#                                 "$set": {"share_status": "inactive"},
#                                 "$set": {"expired": True},
#                             },
#                         )
#                     except:
#                         await db_plex["plex"].update_one(
#                             {"discord_id": discord_id},
#                             {
#                                 "$set": {"share_status": "inactive"},
#                                 "$set": {"expired": True},
#                             },
#                         )

#     print("Subscription update completed.")


# # update_status_loop.start()
# update_subscription_loop.start()

bot.run(DISCORD_TOKEN)
