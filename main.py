import os
import json
import asyncio
import boto3
from typing import List, Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis
from botocore.exceptions import NoCredentialsError

app = FastAPI(title="Kustify API", description="Telegram-like Chat with Voice & Groups", version="2.0")

# --- CONFIGURATION ---

# 1. Redis Configuration (Upstash)
REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
if not REDIS_URL:
    print("WARNING: REDIS_URL not set. App will fail to connect to DB.")

# 2. AWS S3 Configuration (Bucketeer)
AWS_ACCESS_KEY = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("BUCKETEER_AWS_REGION")
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# --- INITIALIZATION ---

# Redis Client
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# S3 Client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Constants
GLOBAL_CHANNEL = "kustify_global_events"
GROUPS_KEY = "kustify:groups_list"

# --- PYDANTIC MODELS ---

class GroupCreate(BaseModel):
    name: str

class Message(BaseModel):
    id: str
    group_id: str
    user_id: str
    user_name: str
    text: Optional[str] = None
    image_url: Optional[str] = None
    timestamp: float

# --- WEBSOCKET CONNECTION MANAGER ---

class ConnectionManager:
    def __init__(self):
        # connections stores: {group_id: [WebSocket, ...]}
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # vc_peers stores: {group_id: {user_id: peer_id}}
        self.vc_peers: Dict[str, Dict[str, str]] = {}

    async def connect(self, websocket: WebSocket, group_id: str):
        await websocket.accept()
        if group_id not in self.active_connections:
            self.active_connections[group_id] = []
        self.active_connections[group_id].append(websocket)

    def disconnect(self, websocket: WebSocket, group_id: str):
        if group_id in self.active_connections:
            if websocket in self.active_connections[group_id]:
                self.active_connections[group_id].remove(websocket)

    async def broadcast_to_group(self, group_id: str, message: dict):
        if group_id in self.active_connections:
            # Serialize once
            text_data = json.dumps(message)
            for connection in self.active_connections[group_id]:
                try:
                    await connection.send_text(text_data)
                except Exception:
                    # Connection might be dead, remove it later or ignore
                    pass

manager = ConnectionManager()

# --- REDIS LISTENER (BACKGROUND TASK) ---
# Listens for messages from other server instances (if scaled) and pushes to WS

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            # 'data' should contain 'group_id' to know where to route
            group_id = data.get("group_id")
            if group_id:
                await manager.broadcast_to_group(group_id, data)

@app.on_event("startup")
async def startup_event():
    # Ensure default group exists
    exists = await redis.sismember(GROUPS_KEY, "General")
    if not exists:
        await redis.sadd(GROUPS_KEY, "General")
    
    asyncio.create_task(redis_listener())

# --- API ENDPOINTS ---

@app.get("/api/groups")
async def get_groups():
    """Fetch all available chat groups."""
    groups = await redis.smembers(GROUPS_KEY)
    return {"groups": list(groups)}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    """Create a new chat group."""
    await redis.sadd(GROUPS_KEY, group.name)
    return {"status": "success", "group": group.name}

@app.get("/api/history/{group_id}")
async def get_history(group_id: str, limit: int = 50):
    """Fetch chat history for a group."""
    key = f"kustify:history:{group_id}"
    # Range is inclusive, fetch last 'limit' items
    messages = await redis.lrange(key, -limit, -1)
    # Parse JSON strings back to objects
    parsed = [json.loads(m) for m in messages]
    return parsed

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """Uploads file to Bucketeer S3 and returns a presigned URL."""
    try:
        file_key = f"kustify_uploads/{file.filename}"
        s3_client.upload_fileobj(
            file.file,
            BUCKET_NAME,
            file_key,
            ExtraArgs={'ContentType': file.content_type, 'ACL': 'public-read'} # Try public read if bucket allows
        )
        
        # Generate URL.
        # Ideally, if bucket is public:
        # url = f"https://{BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{file_key}"
        
        # Safest fallback: Presigned URL
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': file_key},
            ExpiresIn=604800 # 7 days
        )
        return {"url": url}
    except Exception as e:
        # If ACL public-read fails, it might be a private bucket, just proceed with presigned
        return JSONResponse(status_code=500, content={"error": str(e)})

# --- WEBSOCKET ENDPOINT ---

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await manager.connect(websocket, group_id)
    try:
        while True:
            # Wait for data
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            msg_type = data.get("type")

            if msg_type == "heartbeat":
                # Respond to keep connection alive
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))
                continue

            elif msg_type == "message":
                # 1. Construct Message
                out_msg = {
                    "type": "message",
                    "group_id": group_id,
                    "user_id": user_id,
                    "user_name": data.get("user_name", "Unknown"),
                    "text": data.get("text"),
                    "image_url": data.get("image_url"),
                    "timestamp": data.get("timestamp")
                }
                
                # 2. Save to Redis History
                history_key = f"kustify:history:{group_id}"
                await redis.rpush(history_key, json.dumps(out_msg))
                
                # 3. Publish to Redis (triggers listener -> broadcasts to all)
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out_msg))

            elif msg_type == "vc_signal":
                # WebRTC Signaling: Forward signal to specific target or broadcast join
                # data needed: {target_peer_id, signal_data, sender_peer_id}
                signal_payload = {
                    "type": "vc_signal",
                    "sender_id": user_id,
                    "payload": data.get("payload"),
                    "target_id": data.get("target_id") # If None, broadcast
                }
                # For VC, we just reflect it to the group (clients filter by target)
                await manager.broadcast_to_group(group_id, signal_payload)

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id)
        # Notify group user left
        leave_msg = {"type": "user_left", "user_id": user_id}
        await manager.broadcast_to_group(group_id, leave_msg)

# --- FRONTEND (PREMIUM UI) ---

@app.get("/")
async def get():
    return HTMLResponse(html_content)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kustify | Premium Chat</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <style>
        :root {
            --bg-dark: #0f172a;
            --bg-panel: #1e293b;
            --bg-input: #334155;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --border: #334155;
            --msg-me: #3b82f6;
            --msg-other: #1e293b;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; scrollbar-width: thin; scrollbar-color: var(--bg-input) var(--bg-dark); }
        
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-dark);
            color: var(--text-main);
            height: 100vh;
            display: flex;
            overflow: hidden;
        }

        /* SIDEBAR */
        #sidebar {
            width: 280px;
            background-color: #111827;
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
        }

        .brand {
            padding: 20px;
            font-size: 1.5rem;
            font-weight: 800;
            color: var(--primary);
            letter-spacing: -0.5px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .group-list {
            flex: 1;
            overflow-y: auto;
            padding: 10px;
        }

        .group-item {
            padding: 12px 15px;
            margin-bottom: 5px;
            border-radius: 8px;
            cursor: pointer;
            transition: background 0.2s;
            font-weight: 500;
            display: flex;
            align-items: center;
        }

        .group-item:hover { background-color: var(--bg-input); }
        .group-item.active { background-color: var(--primary); color: white; }
        .group-item::before { content: '#'; margin-right: 10px; opacity: 0.5; }

        .create-group-btn {
            margin: 10px;
            padding: 10px;
            background: var(--bg-input);
            border: 1px dashed var(--text-muted);
            color: var(--text-muted);
            border-radius: 8px;
            cursor: pointer;
            text-align: center;
        }
        .create-group-btn:hover { border-color: var(--primary); color: var(--primary); }

        /* MAIN CHAT */
        #chat-area {
            flex: 1;
            display: flex;
            flex-direction: column;
            background-color: var(--bg-dark);
            position: relative;
        }

        #chat-header {
            padding: 15px 25px;
            border-bottom: 1px solid var(--border);
            background-color: rgba(15, 23, 42, 0.95);
            display: flex;
            justify-content: space-between;
            align-items: center;
            backdrop-filter: blur(5px);
        }

        .header-title { font-weight: 700; font-size: 1.1rem; }
        
        .vc-controls button {
            padding: 8px 16px;
            border-radius: 20px;
            border: none;
            font-weight: 600;
            cursor: pointer;
            transition: 0.2s;
        }
        .btn-join-vc { background: #10b981; color: white; }
        .btn-leave-vc { background: #ef4444; color: white; display: none; }

        #messages {
            flex: 1;
            overflow-y: auto;
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .message {
            max-width: 65%;
            padding: 10px 14px;
            border-radius: 12px;
            position: relative;
            line-height: 1.5;
            font-size: 0.95rem;
            animation: fadeIn 0.2s ease;
        }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

        .msg-own {
            align-self: flex-end;
            background-color: var(--msg-me);
            border-bottom-right-radius: 2px;
        }

        .msg-other {
            align-self: flex-start;
            background-color: var(--msg-other);
            border-bottom-left-radius: 2px;
        }

        .msg-meta {
            font-size: 0.75rem;
            opacity: 0.7;
            margin-bottom: 4px;
            display: block;
            font-weight: 600;
        }

        .msg-img {
            max-width: 100%;
            border-radius: 8px;
            margin-top: 8px;
            cursor: pointer;
        }

        #input-area {
            padding: 20px;
            background-color: var(--bg-dark);
            display: flex;
            gap: 10px;
            align-items: center;
        }

        .attach-btn {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            border: none;
            background: var(--bg-input);
            color: var(--text-muted);
            cursor: pointer;
            font-size: 1.2rem;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .attach-btn:hover { color: var(--primary); }

        #msg-input {
            flex: 1;
            background-color: var(--bg-input);
            border: none;
            padding: 12px 20px;
            border-radius: 24px;
            color: white;
            font-size: 1rem;
            outline: none;
        }
        #msg-input::placeholder { color: var(--text-muted); }

        .send-btn {
            background-color: var(--primary);
            color: white;
            border: none;
            width: 45px;
            height: 45px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .send-btn:hover { background-color: var(--primary-hover); }

        /* MODAL */
        #login-modal {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8);
            display: flex; justify-content: center; align-items: center; z-index: 100;
        }
        .modal-content {
            background: var(--bg-panel); padding: 30px; border-radius: 16px;
            width: 300px; text-align: center;
        }
        .modal-content input {
            width: 100%; padding: 10px; margin: 15px 0; border-radius: 8px;
            border: 1px solid var(--border); background: var(--bg-dark); color: white;
        }
        .modal-content button {
            width: 100%; padding: 10px; background: var(--primary);
            border: none; color: white; border-radius: 8px; font-weight: bold; cursor: pointer;
        }
    </style>
</head>
<body>

    <div id="login-modal">
        <div class="modal-content">
            <h2>Welcome to Kustify</h2>
            <p style="color:var(--text-muted); margin-top:5px;">Enter your name to join</p>
            <input type="text" id="username-input" placeholder="Your Name">
            <button onclick="login()">Start Chatting</button>
        </div>
    </div>

    <div id="sidebar">
        <div class="brand">Kustify</div>
        <div class="group-list" id="group-list">
            </div>
        <div class="create-group-btn" onclick="createNewGroup()">+ New Group</div>
    </div>

    <div id="chat-area">
        <div id="chat-header">
            <div class="header-title" id="current-group-name"># General</div>
            <div class="vc-controls">
                <button id="btn-join-vc" class="btn-join-vc" onclick="joinVoice()">🎤 Join Voice</button>
                <button id="btn-leave-vc" class="btn-leave-vc" onclick="leaveVoice()">❌ Leave</button>
            </div>
        </div>

        <div id="messages"></div>

        <div id="input-area">
            <button class="attach-btn" onclick="document.getElementById('file-input').click()">📎</button>
            <input type="file" id="file-input" style="display: none;" onchange="handleFileUpload()">
            <input type="text" id="msg-input" placeholder="Write a message..." autocomplete="off">
            <button class="send-btn" onclick="sendMessage()">➤</button>
        </div>
    </div>

    <div id="audio-container" style="display:none;"></div>

    <script>
        let currentUser = null;
        let userId = "user_" + Math.random().toString(36).substr(2, 9);
        let currentGroup = "General";
        let ws = null;
        let heartbeatInterval = null;
        
        // Voice Chat Globals
        let peer = null;
        let myStream = null;
        let peers = {}; // Keep track of active calls

        // --- INIT & LOGIN ---
        
        async function login() {
            const input = document.getElementById('username-input');
            if (!input.value.trim()) return;
            currentUser = input.value;
            document.getElementById('login-modal').style.display = 'none';
            await loadGroups();
            connectToGroup("General");
        }

        async function loadGroups() {
            const res = await fetch('/api/groups');
            const data = await res.json();
            const list = document.getElementById('group-list');
            list.innerHTML = '';
            data.groups.forEach(g => {
                const div = document.createElement('div');
                div.className = `group-item ${g === currentGroup ? 'active' : ''}`;
                div.innerText = g;
                div.onclick = () => connectToGroup(g);
                list.appendChild(div);
            });
        }

        async function createNewGroup() {
            const name = prompt("Enter group name:");
            if (name) {
                await fetch('/api/groups', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                });
                loadGroups();
            }
        }

        // --- WEBSOCKET & CHAT ---

        function connectToGroup(groupName) {
            // Clean up old connection
            if (ws) ws.close();
            if (heartbeatInterval) clearInterval(heartbeatInterval);
            leaveVoice(); // Auto leave VC when switching rooms

            currentGroup = groupName;
            document.getElementById('current-group-name').innerText = "# " + groupName;
            
            // Update Sidebar UI
            loadGroups(); 
            
            // Clear Chat
            document.getElementById('messages').innerHTML = '';

            // Load History
            fetch(`/api/history/${groupName}`)
                .then(r => r.json())
                .then(msgs => {
                    msgs.forEach(displayMessage);
                });

            // Connect WS
            const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            ws = new WebSocket(`${proto}://${window.location.host}/ws/${groupName}/${userId}`);

            ws.onopen = () => {
                // Start Heartbeat
                heartbeatInterval = setInterval(() => {
                    ws.send(JSON.stringify({type: "heartbeat"}));
                }, 30000); // 30s
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                if (data.type === "heartbeat_ack") return;
                
                if (data.type === "message") {
                    displayMessage(data);
                } 
                else if (data.type === "vc_signal") {
                    handleVcSignal(data);
                }
            };
        }

        function displayMessage(data) {
            const container = document.getElementById('messages');
            const div = document.createElement('div');
            const isMe = data.user_id === userId;
            
            div.className = `message ${isMe ? 'msg-own' : 'msg-other'}`;
            
            let content = `<span class="msg-meta">${data.user_name}</span>`;
            if (data.text) content += `<div>${data.text}</div>`;
            if (data.image_url) content += `<img src="${data.image_url}" class="msg-img" onclick="window.open(this.src)">`;
            
            div.innerHTML = content;
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
        }

        function sendMessage() {
            const input = document.getElementById('msg-input');
            const text = input.value.trim();
            if (!text) return;

            const msg = {
                type: "message",
                user_name: currentUser,
                text: text,
                timestamp: Date.now()
            };
            ws.send(JSON.stringify(msg));
            input.value = '';
        }

        document.getElementById('msg-input').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') sendMessage();
        });

        async function handleFileUpload() {
            const input = document.getElementById('file-input');
            if (!input.files.length) return;
            
            const file = input.files[0];
            const formData = new FormData();
            formData.append('file', file);

            const statusInput = document.getElementById('msg-input');
            const originalPlace = statusInput.placeholder;
            statusInput.placeholder = "Uploading...";
            statusInput.disabled = true;

            try {
                const res = await fetch('/api/upload', {method: 'POST', body: formData});
                const data = await res.json();
                
                if (data.url) {
                    ws.send(JSON.stringify({
                        type: "message",
                        user_name: currentUser,
                        text: "",
                        image_url: data.url,
                        timestamp: Date.now()
                    }));
                }
            } catch (e) {
                alert("Upload failed");
            } finally {
                input.value = '';
                statusInput.placeholder = originalPlace;
                statusInput.disabled = false;
                statusInput.focus();
            }
        }

        // --- VOICE CHAT (WEB RTC via PeerJS) ---

        function joinVoice() {
            navigator.mediaDevices.getUserMedia({ audio: true, video: false })
                .then(stream => {
                    myStream = stream;
                    
                    // Init Peer (we use the userId as peerId for simplicity)
                    peer = new Peer(userId);

                    peer.on('open', (id) => {
                        console.log('My peer ID is: ' + id);
                        document.getElementById('btn-join-vc').style.display = 'none';
                        document.getElementById('btn-leave-vc').style.display = 'inline-block';
                        
                        // Signal others I joined
                        ws.send(JSON.stringify({
                            type: "vc_signal",
                            payload: { event: "join-request" },
                            target_id: null // Broadcast
                        }));
                    });

                    // Answer incoming calls
                    peer.on('call', (call) => {
                        call.answer(stream);
                        const audio = document.createElement('audio');
                        call.on('stream', userStream => {
                            addAudioStream(audio, userStream);
                        });
                        peers[call.peer] = call;
                    });
                })
                .catch(err => {
                    console.error("Failed to get local stream", err);
                    alert("Could not access microphone.");
                });
        }

        function handleVcSignal(data) {
            // If I am not in VC, ignore signals
            if (!peer || !myStream) return;
            
            const senderId = data.sender_id;
            if (senderId === userId) return;

            const payload = data.payload;

            // Someone new joined, call them
            if (payload.event === "join-request") {
                const call = peer.call(senderId, myStream);
                const audio = document.createElement('audio');
                call.on('stream', userStream => {
                    addAudioStream(audio, userStream);
                });
                peers[senderId] = call;
            }
        }

        function addAudioStream(audio, stream) {
            audio.srcObject = stream;
            audio.addEventListener('loadedmetadata', () => {
                audio.play();
            });
            document.getElementById('audio-container').appendChild(audio);
        }

        function leaveVoice() {
            if (peer) peer.destroy();
            if (myStream) myStream.getTracks().forEach(track => track.stop());
            
            peer = null;
            myStream = null;
            peers = {};
            document.getElementById('audio-container').innerHTML = '';
            
            document.getElementById('btn-join-vc').style.display = 'inline-block';
            document.getElementById('btn-leave-vc').style.display = 'none';
        }

        // Initial Focus
        document.getElementById('username-input').focus();
    </script>
</body>
</html>
"""
