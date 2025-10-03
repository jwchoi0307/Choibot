import discord
from discord import app_commands
import asyncio
import websockets
import json
import logging
import uuid
import aiohttp
import os # Keep this


# --- Configuration ---
logging.basicConfig(level=logging.INFO)
KOYEB_URL = os.environ.get('KOYEB_URL')

# --- Fly.io-friendly Configuration ---
# Read secrets from environment variables set via `fly secrets set`
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
SERVER_ID_STR = os.environ.get('SERVER_ID')
CHANNEL_ID_STR = os.environ.get('CHANNEL_ID')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')

# Define a standard internal port. 8080 is a common choice.
# Fly.io will map public ports (80, 443) to this internal port.
INTERNAL_PORT = int(os.environ.get('INTERNAL_PORT', 8080))
WEBSOCKET_HOST = "0.0.0.0" # This MUST be 0.0.0.0 to listen inside the container

# Check if essential secrets are loaded
if not all([DISCORD_TOKEN, SERVER_ID_STR, CHANNEL_ID_STR]):
    logging.error("FATAL: One or more required environment variables (DISCORD_TOKEN, SERVER_ID, CHANNEL_ID) are not set. Exiting.")
    exit()

# Convert IDs to integers
try:
    SERVER_ID = int(SERVER_ID_STR)
    CHANNEL_ID = int(CHANNEL_ID_STR)
except ValueError as e:
    logging.error(f"FATAL: Could not convert SERVER_ID or CHANNEL_ID to an integer: {e}. Exiting.")
    exit()

# --- Bot State ---
# This set will store the active WebSocket connection to the Minecraft mod
minecraft_client = None
bot_channel = None
# 요청 ID별로 응답을 기다리는 이벤트를 저장할 딕셔너리
pending_requests = {}

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

from websockets.http import Headers # Make sure this is imported

async def http_handler(path, request_headers):
    """
    Handles HTTP health check requests from Fly.io.
    If the request path is our health check endpoint, return a 200 OK response.
    Otherwise, let the WebSocket handshake continue.
    """
    if path == "/health":
        return HTTPResponse(200, Headers({"Content-Type": "text/plain"}), b"OK\n")
    return None # Let websockets handle the connection as normal

async def ping_self():
  """
  Koyeb의 scale-to-zero 정책으로 봇이 잠드는 것을 방지하기 위해
  주기적으로 자신의 Health Check API를 호출합니다.
  """
  await client.wait_until_ready()
  while not client.is_closed():
    # 3분(180초)마다 한 번씩 실행합니다.
    await asyncio.sleep(180)
    if KOYEB_URL: # KOYEB_URL이 설정된 경우에만 실행
      try:
        # aiohttp 세션을 사용하여 자신의 Health Check 엔드포인트에 GET 요청을 보냅니다.
        async with aiohttp.ClientSession() as session:
          # '/health' 경로를 명시적으로 추가해줍니다.
          async with session.get(KOYEB_URL + "/health") as response:
            if response.status == 200:
              logging.info("Successfully pinged self to stay awake.")
            else:
              logging.warning(f"Self-ping failed with status: {response.status}")
      except Exception as e:
        logging.error(f"An error occurred during self-ping: {e}")

# --- WebSocket Server Logic ---
async def websocket_handler(websocket):
    """Handles WebSocket connections from the Minecraft mod."""
    global minecraft_client
    minecraft_client = websocket
    logging.info("Minecraft mod connected.")
    
    # Send server online message to Discord
    if bot_channel:
        embed = discord.Embed(description="✅ **서버가 시작되었습니다!**", color=discord.Color.green())
        await bot_channel.send(embed=embed)

    try:
        # Listen for messages from the mod
        async for message in websocket:
            try:
                data = json.loads(message)
                await process_minecraft_event(data)
            except json.JSONDecodeError:
                logging.error(f"Received invalid JSON: {message}")
            except Exception as e:
                logging.error(f"Error processing message: {e}")
    except websockets.exceptions.ConnectionClosed:
        logging.info("Minecraft mod disconnected.")
    finally:
        minecraft_client = None
        # Send server offline message to Discord
        if bot_channel:
            embed = discord.Embed(description="🛑 **서버가 정지되었습니다!**", color=discord.Color.red())
            await bot_channel.send(embed=embed)

async def process_minecraft_event(data):
    """Processes events sent from the Minecraft mod and posts them to Discord."""
    event_type = data.get("type")
    
    if not bot_channel:
        return

    if event_type == "chat":
        # 웹훅 URL이 설정되지 않았다면 아무것도 하지 않습니다.
        if not WEBHOOK_URL:
            logging.warning("WEBHOOK_URL is not set. Cannot send chat message.")
            return

        player_name = data['player']
        player_uuid = data['uuid']
        message = data['message']
        avatar_url = f"https://api.mineatar.io/face/{player_uuid}"

        # 웹훅에 보낼 payload를 구성합니다.
        payload = {
            "username": player_name,
            "avatar_url": avatar_url,
            "content": message
        }

        # aiohttp를 사용하여 비동기적으로 웹훅에 POST 요청을 보냅니다.
        async with aiohttp.ClientSession() as session:
            async with session.post(WEBHOOK_URL, json=payload) as response:
                if not response.ok:
                    logging.error(f"Failed to send webhook message: {response.status} {await response.text()}")
        


    elif event_type == "join":
        player_name = data['player']
        player_uuid = data['uuid']
        avatar_url = f"https://api.mineatar.io/face/{player_uuid}"
        
        # description을 제거하고, author의 name에 모든 텍스트를 넣습니다.
        author_text = f"{player_name}님이 서버에 참여하였습니다."
        embed = discord.Embed(color=discord.Color.green())
        embed.set_author(name=author_text, icon_url=avatar_url)
        await bot_channel.send(embed=embed)

    elif event_type == "leave":
        player_name = data['player']
        player_uuid = data['uuid']
        avatar_url = f"https://api.mineatar.io/face/{player_uuid}"

        # description을 제거하고, author의 name에 모든 텍스트를 넣습니다.
        author_text = f"{player_name}님이 서버를 떠났습니다."
        embed = discord.Embed(color=discord.Color.red())
        embed.set_author(name=author_text, icon_url=avatar_url)
        await bot_channel.send(embed=embed)
        
    elif event_type == "death":
        death_message = data['message']
        player_uuid = data['uuid']
        avatar_url = f"https://api.mineatar.io/face/{player_uuid}"
        
        # description을 제거하고, author의 name에 모든 텍스트를 넣습니다.
        # 사망 메시지 자체에 이미 플레이어 이름이 포함되어 있습니다.
        author_text = f"{death_message}"
        embed = discord.Embed(color=discord.Color.dark_red())
        embed.set_author(name=author_text, icon_url=avatar_url)
        await bot_channel.send(embed=embed)

    if event_type in ["list_response", "tps_response"]:
        request_id = data.get("request_id")
        if request_id in pending_requests:
            # 해당 요청에 대한 응답 데이터를 저장하고, 이벤트(Event)를 set()하여 기다림을 해제
            event, storage = pending_requests[request_id]
            storage['response'] = data
            event.set()
        return # 다른 처리는 필요 없으므로 여기서 종료
    
    if not bot_channel:
        return
    

async def start_websocket_server():
    """
    HTTP 요청을 먼저 처리하고, 나머지를 웹소켓으로 넘기는 서버를 시작합니다.
    """
    async with serve(
        websocket_handler,
        WEBSOCKET_HOST,
        PORT,
        process_request=http_handler
    ):
        logging.info(f"WebSocket server started on {WEBSOCKET_HOST}:{PORT} and ready for health checks at /healthz.")
        await asyncio.Future()

# --- Discord Event Handlers ---
@client.event
async def on_ready():
    """Called when the bot is ready and connected to Discord."""
    global bot_channel
    bot_channel = client.get_channel(CHANNEL_ID)
    if not bot_channel:
        logging.error(f"Could not find channel with ID {CHANNEL_ID}")
        return
        
    await tree.sync(guild=discord.Object(id=SERVER_ID))
    logging.info(f'Logged in as {client.user}')

@client.event
async def on_message(message):
    """Called when a message is sent in a channel the bot can see."""
    # Ignore messages from the bot itself or from other channels
    if message.author == client.user or message.channel.id != CHANNEL_ID:
        return

    if minecraft_client and not message.author.bot:
        # Forward the message to the Minecraft mod
        payload = {
            "type": "discord_chat",
            "author": message.author.display_name,
            "message": message.content
        }
        await minecraft_client.send(json.dumps(payload))

# --- Discord Slash Commands ---
@tree.command(name="list", description="Lists current players on the Minecraft server.", guild=discord.Object(id=SERVER_ID))
async def list_command(interaction: discord.Interaction):
    """Handles the /list command."""
    if not minecraft_client:
        await interaction.response.send_message("서버가 연결되지 않았습니다.", ephemeral=False)
        return

    await interaction.response.defer(ephemeral=False)

    
    
    request_id = str(uuid.uuid4())
    request = {"type": "get_list", "request_id": request_id}
    
    # 응답을 기다리기 위한 이벤트와 데이터 저장 공간 생성
    event = asyncio.Event()
    storage = {}
    pending_requests[request_id] = (event, storage)

    try:
        await minecraft_client.send(json.dumps(request))
        # 이벤트가 set() 되거나 타임아웃될 때까지 최대 5초간 기다림
        await asyncio.wait_for(event.wait(), timeout=5.0)
        
        response = storage.get('response')
        if response:
            count = response['count']
            max_players = response['max']
            players = response['players']
            
            embed = discord.Embed(title="플레이어 수", description=f"**{count}/{max_players}** 명이 온라인입니다.", color=discord.Color.blue())
            if players:
                embed.add_field(name="플레이어 명단", value="\n".join(players), inline=False)
            
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send("예상하지 못한 응답 수신.", ephemeral=False)

    except asyncio.TimeoutError:
        await interaction.followup.send("서버가 응답하지 않습니다.", ephemeral=False)
    except Exception as e:
        await interaction.followup.send(f"오류 발생: {e}", ephemeral=False)
    finally:
        # 완료되었거나 실패한 요청을 딕셔너리에서 제거
        pending_requests.pop(request_id, None)

@tree.command(name="tps", description="서버의 tps를 보여줍니다.", guild=discord.Object(id=SERVER_ID))
async def tps_command(interaction: discord.Interaction):
    if not minecraft_client:
        await interaction.response.send_message("서버가 연결되지 않았습니다.", ephemeral=False)
        return

    await interaction.response.defer(ephemeral=False)
    
    request_id = str(uuid.uuid4())
    request = {"type": "get_tps", "request_id": request_id}
    
    event = asyncio.Event()
    storage = {}
    pending_requests[request_id] = (event, storage)

    try:
        await minecraft_client.send(json.dumps(request))
        await asyncio.wait_for(event.wait(), timeout=5.0)
        
        response = storage.get('response')
        if response:
            embed = discord.Embed(title="서버 tps (Ticks Per Second)", color=discord.Color.purple())
            for dimension, tps in response['dimensions'].items():
                embed.add_field(name=dimension.replace("_", " ").title(), value=f"`{tps:.2f}` TPS", inline=True)
            await interaction.followup.send(embed=embed)
        else:
             await interaction.followup.send("Received an unexpected response from the server.", ephemeral=True)

    except asyncio.TimeoutError:
        await interaction.followup.send("The server did not respond in time.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)
    finally:
        pending_requests.pop(request_id, None)

# --- Main Execution ---
async def main():
    """Main function to run the bot and WebSocket server concurrently."""
    await asyncio.gather(
        client.start(DISCORD_TOKEN),
        start_websocket_server(),
        ping_self()
    )

if __name__ == "__main__":
    asyncio.run(main())
