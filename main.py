import os
import json
import asyncio
import boto3
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from redis import asyncio as aioredis
from botocore.exceptions import NoCredentialsError

app = FastAPI()

# --- CONFIGURATION (STRICTLY USING YOUR LOGGED VARS) ---

# 1. Redis Configuration (Upstash)
REDIS_URL = os.getenv("UPSTASH_REDIS_URL")
if not REDIS_URL:
    # Fallback if only REST vars are present, though URL is preferred for clients
    REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")

# 2. AWS S3 Configuration (Bucketeer)
AWS_ACCESS_KEY = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("BUCKETEER_AWS_REGION")
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# --- INITIALIZATION ---

# Initialize Redis
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# Initialize S3 Client (Boto3)
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Constants
CHANNEL = "telegram_global_chat"
HISTORY_KEY = "telegram_chat_history"

# --- HTML FRONTEND (Embedded for single-file deployment) ---
html = """
<!DOCTYPE html>
<html>
    <head>
        <title>Python Telegram Clone</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0e1621; color: white; margin: 0; display: flex; flex-direction: column; height: 100vh; }
            #header { padding: 15px; background: #17212b; border-bottom: 1px solid #000; font-weight: bold; }
            #messages { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 8px; }
            .message { max-width: 70%; padding: 8px 12px; border-radius: 8px; position: relative; word-wrap: break-word; }
            .my-msg { align-self: flex-end; background: #2b5278; }
            .other-msg { align-self: flex-start; background: #182533; }
            .meta { font-size: 0.75em; color: #6c7883; margin-bottom: 4px; }
            .content { font-size: 1em; }
            .media-img { max-width: 100%; border-radius: 4px; margin-top: 5px; cursor: pointer; }
            #input-area { padding: 10px; background: #17212b; display: flex; gap: 10px; align-items: center; }
            input[type="text"] { flex: 1; padding: 10px; border-radius: 20px; border: none; background: #0e1621; color: white; outline: none; }
            button { padding: 10px 20px; border: none; border-radius: 20px; background: #5288c1; color: white; cursor: pointer; font-weight: bold; }
            #file-label { cursor: pointer; color: #5288c1; font-size: 24px; padding: 0 10px; }
        </style>
    </head>
    <body>
        <div id="header">Telegram Clone (Upstash + Bucketeer)</div>
        <div id="messages"></div>
        <div id="input-area">
            <label id="file-label" for="file-input">📎</label>
            <input type="file" id="file-input" style="display: none;" onchange="uploadFile()">
            <input type="text" id="messageText" placeholder="Write a message..." autocomplete="off"/>
            <button onclick="sendMessage()">Send</button>
        </div>

        <script>
            // Generate a random user ID
            const userId = "User_" + Math.floor(Math.random() * 1000);
            
            // Connect to WebSocket
            const protocol = window.location.protocol === "https:" ? "wss" : "ws";
            const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${userId}`);

            ws.onmessage = function(event) {
                const data = JSON.parse(event.data);
                displayMessage(data);
            };

            function displayMessage(data) {
                const messages = document.getElementById('messages');
                const msgDiv = document.createElement('div');
                const isMe = data.user === userId;
                
                msgDiv.className = `message ${isMe ? 'my-msg' : 'other-msg'}`;
                
                let contentHtml = `<div class="content">${data.text || ''}</div>`;
                
                // If message has an image URL (from Bucketeer)
                if (data.image_url) {
                    contentHtml += `<img src="${data.image_url}" class="media-img" onclick="window.open(this.src)">`;
                }

                msgDiv.innerHTML = `<div class="meta">${data.user}</div>${contentHtml}`;
                messages.appendChild(msgDiv);
                messages.scrollTop = messages.scrollHeight;
            }

            function sendMessage() {
                const input = document.getElementById("messageText");
                if (input.value.trim() === "") return;
                
                const msg = {
                    user: userId,
                    text: input.value,
                    image_url: null
                };
                ws.send(JSON.stringify(msg));
                input.value = '';
            }

            // Handle Enter key
            document.getElementById("messageText").addEventListener("keypress", function(event) {
                if (event.key === "Enter") sendMessage();
            });

            async function uploadFile() {
                const fileInput = document.getElementById("file-input");
                if (fileInput.files.length === 0) return;

                const file = fileInput.files[0];
                const formData = new FormData();
                formData.append("file", file);

                // Show uploading state
                const input = document.getElementById("messageText");
                const originalPlaceholder = input.placeholder;
                input.placeholder = "Uploading to Bucketeer...";
                input.disabled = true;

                try {
                    const response = await fetch("/upload", {
                        method: "POST",
                        body: formData
                    });
                    const result = await response.json();
                    
                    if (result.url) {
                        // Send message with image URL
                        const msg = {
                            user: userId,
                            text: "",
                            image_url: result.url
                        };
                        ws.send(JSON.stringify(msg));
                    } else {
                        alert("Upload failed");
                    }
                } catch (e) {
                    console.error(e);
                    alert("Error uploading file");
                } finally {
                    fileInput.value = "";
                    input.placeholder = originalPlaceholder;
                    input.disabled = false;
                    input.focus();
                }
            }
        </script>
    </body>
</html>
"""

# --- BACKEND LOGIC ---

@app.get("/")
async def get():
    return HTMLResponse(html)

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    Uploads file to Bucketeer S3 and returns a presigned URL.
    """
    try:
        file_key = f"uploads/{file.filename}"
        
        # 1. Upload to Bucketeer
        s3_client.upload_fileobj(
            file.file,
            BUCKET_NAME,
            file_key,
            ExtraArgs={'ContentType': file.content_type}
        )

        # 2. Generate a Presigned URL (valid for 1 hour) for viewing
        # This handles cases where the bucket might be private
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': file_key},
            ExpiresIn=3600
        )
        
        return {"url": url}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        # Broadcast to all connected websockets
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

async def redis_listener():
    """
    Background task: Listens to Redis Pub/Sub channel and broadcasts to WebSockets.
    This ensures that if you have multiple server instances, they all sync.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            await manager.broadcast(message["data"])

@app.on_event("startup")
async def startup_event():
    # Start the Redis listener in the background
    asyncio.create_task(redis_listener())

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket)
    
    # 1. Send Chat History on Connect (from Upstash Redis)
    try:
        # Get last 50 messages
        history = await redis.lrange(HISTORY_KEY, -50, -1)
        for msg in history:
            await websocket.send_text(msg)
    except Exception as e:
        print(f"Error fetching history: {e}")

    try:
        while True:
            # 2. Wait for message from Client
            data = await websocket.receive_text()
            
            # 3. Publish to Redis (Upstash)
            # This triggers the redis_listener, which then updates all clients
            await redis.publish(CHANNEL, data)
            
            # 4. Save to History (Upstash)
            await redis.rpush(HISTORY_KEY, data)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)
