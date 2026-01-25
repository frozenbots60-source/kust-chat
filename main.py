import os
import json
import asyncio
import boto3
from typing import List, Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis
from botocore.exceptions import NoCredentialsError

app = FastAPI(title="Kustify API", description="Telegram-like Chat with Voice & Groups", version="3.0")

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
            text_data = json.dumps(message)
            for connection in self.active_connections[group_id]:
                try:
                    await connection.send_text(text_data)
                except Exception:
                    pass

manager = ConnectionManager()

# --- REDIS LISTENER (BACKGROUND TASK) ---

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
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
    groups = await redis.smembers(GROUPS_KEY)
    return {"groups": list(groups)}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    await redis.sadd(GROUPS_KEY, group.name)
    return {"status": "success", "group": group.name}

@app.get("/api/history/{group_id}")
async def get_history(group_id: str, limit: int = 50):
    key = f"kustify:history:{group_id}"
    messages = await redis.lrange(key, -limit, -1)
    parsed = [json.loads(m) for m in messages]
    return parsed

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    FIXED: Removed ACL='public-read' to prevent 500 errors on private buckets.
    Uses Presigned URLs for access.
    """
    try:
        file_key = f"kustify_uploads/{file.filename}"
        
        # Upload without ACL (safer for Bucketeer)
        s3_client.upload_fileobj(
            file.file,
            BUCKET_NAME,
            file_key,
            ExtraArgs={'ContentType': file.content_type}
        )
        
        # Generate Presigned URL (valid for 7 days)
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': file_key},
            ExpiresIn=604800 
        )
        return {"url": url}
    except Exception as e:
        print(f"Upload Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# --- WEBSOCKET ENDPOINT ---

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await manager.connect(websocket, group_id)
    try:
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)
            msg_type = data.get("type")

            if msg_type == "heartbeat":
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))
                continue

            elif msg_type == "message":
                out_msg = {
                    "type": "message",
                    "group_id": group_id,
                    "user_id": user_id,
                    "user_name": data.get("user_name", "Unknown"),
                    "text": data.get("text"),
                    "image_url": data.get("image_url"),
                    "timestamp": data.get("timestamp")
                }
                
                # Save & Broadcast
                history_key = f"kustify:history:{group_id}"
                await redis.rpush(history_key, json.dumps(out_msg))
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out_msg))

            elif msg_type == "vc_signal":
                # WebRTC Signal Routing
                signal_payload = {
                    "type": "vc_signal",
                    "sender_id": user_id,
                    "sender_name": data.get("sender_name"),
                    "payload": data.get("payload"),
                    "target_id": data.get("target_id")
                }
                await manager.broadcast_to_group(group_id, signal_payload)
            
            elif msg_type == "vc_update":
                # For mic status updates (mute/unmute/speaking)
                update_payload = {
                    "type": "vc_update",
                    "sender_id": user_id,
                    "status": data.get("status") # e.g., { "muted": true }
                }
                await manager.broadcast_to_group(group_id, update_payload)

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id)
        # Notify others user left VC/Chat
        leave_msg = {"type": "user_left", "user_id": user_id}
        await manager.broadcast_to_group(group_id, leave_msg)

# --- FRONTEND (HTML/CSS/JS) ---

@app.get("/")
async def get():
    return HTMLResponse(html_content)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
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
            --sidebar-width: 280px;
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

        /* --- SIDEBAR & MOBILE NAV --- */
        #sidebar {
            width: var(--sidebar-width);
            background-color: #111827;
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            transition: transform 0.3s ease;
            z-index: 50;
        }

        .brand {
            padding: 20px;
            font-size: 1.5rem;
            font-weight: 800;
            color: var(--primary);
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .close-sidebar { display: none; cursor: pointer; font-size: 1.2rem; }

        .group-list { flex: 1; overflow-y: auto; padding: 10px; }
        .group-item {
            padding: 12px 15px; margin-bottom: 5px; border-radius: 8px;
            cursor: pointer; font-weight: 500; display: flex; align-items: center;
            color: var(--text-muted);
        }
        .group-item:hover { background-color: var(--bg-input); color: white; }
        .group-item.active { background-color: var(--primary); color: white; }
        .group-item::before { content: '#'; margin-right: 10px; opacity: 0.5; }

        .create-group-btn {
            margin: 10px; padding: 10px; background: var(--bg-input);
            border: 1px dashed var(--text-muted); color: var(--text-muted);
            border-radius: 8px; cursor: pointer; text-align: center;
        }

        /* --- MAIN CHAT AREA --- */
        #chat-area {
            flex: 1; display: flex; flex-direction: column;
            background-color: var(--bg-dark); position: relative;
            width: 100%;
        }

        #chat-header {
            padding: 15px 20px; border-bottom: 1px solid var(--border);
            background-color: rgba(15, 23, 42, 0.95);
            display: flex; justify-content: space-between; align-items: center;
            backdrop-filter: blur(5px);
        }

        .header-left { display: flex; align-items: center; gap: 10px; }
        .menu-btn { 
            display: none; background: none; border: none; color: white; 
            font-size: 1.5rem; cursor: pointer; 
        }
        .header-title { font-weight: 700; font-size: 1.1rem; }

        .vc-btn {
            padding: 8px 16px; border-radius: 20px; border: none;
            font-weight: 600; cursor: pointer; background: #10b981; color: white;
            display: flex; align-items: center; gap: 5px;
        }

        #messages {
            flex: 1; overflow-y: auto; padding: 20px;
            display: flex; flex-direction: column; gap: 12px;
        }

        .message {
            max-width: 80%; padding: 10px 14px; border-radius: 12px;
            position: relative; line-height: 1.5; font-size: 0.95rem;
            animation: fadeIn 0.2s ease;
        }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

        .msg-own { align-self: flex-end; background-color: var(--msg-me); border-bottom-right-radius: 2px; }
        .msg-other { align-self: flex-start; background-color: var(--msg-other); border-bottom-left-radius: 2px; }
        .msg-meta { font-size: 0.75rem; opacity: 0.7; margin-bottom: 4px; display: block; font-weight: 600; }

        /* Image Loading */
        .img-container {
            position: relative; min-width: 150px; min-height: 150px;
            background: rgba(0,0,0,0.2); border-radius: 8px; margin-top: 8px;
            display: flex; align-items: center; justify-content: center; overflow: hidden;
        }
        .msg-img { max-width: 100%; display: none; border-radius: 8px; cursor: pointer; }
        .loading-spinner {
            border: 3px solid rgba(255,255,255,0.3); border-radius: 50%;
            border-top: 3px solid white; width: 24px; height: 24px;
            animation: spin 1s linear infinite; position: absolute;
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

        #input-area {
            padding: 15px; background-color: var(--bg-dark);
            display: flex; gap: 10px; align-items: center; border-top: 1px solid var(--border);
        }
        .attach-btn {
            width: 40px; height: 40px; border-radius: 50%; border: none;
            background: var(--bg-input); color: var(--text-muted); cursor: pointer;
            font-size: 1.2rem;
        }
        #msg-input {
            flex: 1; background-color: var(--bg-input); border: none;
            padding: 12px 20px; border-radius: 24px; color: white;
            font-size: 1rem; outline: none;
        }
        .send-btn {
            background-color: var(--primary); color: white; border: none;
            width: 45px; height: 45px; border-radius: 50%; cursor: pointer;
        }

        /* --- VC MODAL (Telegram Style) --- */
        #vc-modal {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.85); z-index: 100; display: none;
            flex-direction: column; align-items: center; justify-content: center;
        }
        .vc-window {
            background: var(--bg-panel); width: 90%; max-width: 400px;
            border-radius: 20px; padding: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            display: flex; flex-direction: column; gap: 20px;
        }
        .vc-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
        .vc-title { font-weight: bold; font-size: 1.2rem; }
        .vc-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
            gap: 15px; max-height: 300px; overflow-y: auto; padding: 10px 0;
        }
        .vc-user {
            display: flex; flex-direction: column; align-items: center; gap: 5px;
            position: relative;
        }
        .vc-avatar {
            width: 60px; height: 60px; border-radius: 50%; background: linear-gradient(135deg, #6366f1, #a855f7);
            display: flex; align-items: center; justify-content: center; font-size: 1.5rem; font-weight: bold;
            border: 3px solid transparent; transition: border-color 0.2s;
        }
        .vc-avatar.speaking { border-color: #10b981; box-shadow: 0 0 15px rgba(16, 185, 129, 0.4); }
        .vc-avatar.muted { filter: grayscale(100%); opacity: 0.6; }
        .vc-name { font-size: 0.8rem; text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
        .vc-mic-icon {
            position: absolute; bottom: 20px; right: 5px; background: #ef4444;
            width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center; justify-content: center;
            font-size: 0.7rem; border: 2px solid var(--bg-panel);
        }
        .vc-controls-row { display: flex; justify-content: center; gap: 20px; margin-top: 10px; }
        .vc-circle-btn {
            width: 50px; height: 50px; border-radius: 50%; border: none;
            display: flex; align-items: center; justify-content: center; font-size: 1.2rem; cursor: pointer;
        }
        .btn-mute { background: var(--bg-input); color: white; }
        .btn-mute.active { background: white; color: black; }
        .btn-end { background: #ef4444; color: white; }

        /* --- LOGIN MODAL --- */
        #login-modal {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.9); z-index: 200;
            display: flex; justify-content: center; align-items: center;
        }
        .modal-content {
            background: var(--bg-panel); padding: 30px; border-radius: 16px;
            width: 300px; text-align: center;
        }
        .modal-content input {
            width: 100%; padding: 12px; margin: 15px 0; border-radius: 8px;
            border: 1px solid var(--border); background: var(--bg-dark); color: white; outline: none;
        }
        .modal-content button {
            width: 100%; padding: 12px; background: var(--primary);
            border: none; color: white; border-radius: 8px; font-weight: bold; cursor: pointer;
        }

        /* --- RESPONSIVE CSS --- */
        @media (max-width: 768px) {
            #sidebar {
                position: absolute; height: 100%; transform: translateX(-100%);
                box-shadow: 5px 0 15px rgba(0,0,0,0.5);
            }
            #sidebar.open { transform: translateX(0); }
            .menu-btn { display: block; }
            .close-sidebar { display: block; }
            .message { max-width: 90%; }
        }
    </style>
</head>
<body>

    <div id="login-modal">
        <div class="modal-content">
            <h2>Kustify</h2>
            <p style="color:var(--text-muted); margin-top:5px;">Enter your name</p>
            <input type="text" id="username-input" placeholder="Your Name">
            <button onclick="login()">Start Messaging</button>
        </div>
    </div>

    <div id="vc-modal">
        <div class="vc-window">
            <div class="vc-header">
                <span class="vc-title">Voice Chat</span>
                <span style="color:#10b981; font-size:0.8rem;">● Live</span>
            </div>
            <div class="vc-grid" id="vc-grid">
                </div>
            <div class="vc-controls-row">
                <button class="vc-circle-btn btn-mute" id="mute-btn" onclick="toggleMute()">🎤</button>
                <button class="vc-circle-btn btn-end" onclick="leaveVoice()">❌</button>
            </div>
        </div>
    </div>

    <div id="sidebar">
        <div class="brand">
            Kustify
            <span class="close-sidebar" onclick="toggleSidebar()">✕</span>
        </div>
        <div class="group-list" id="group-list"></div>
        <div class="create-group-btn" onclick="createNewGroup()">+ New Group</div>
    </div>

    <div id="chat-area">
        <div id="chat-header">
            <div class="header-left">
                <button class="menu-btn" onclick="toggleSidebar()">☰</button>
                <div class="header-title" id="current-group-name"># General</div>
            </div>
            <button class="vc-btn" onclick="joinVoice()">🎤 Join VC</button>
        </div>

        <div id="messages"></div>

        <div id="input-area">
            <button class="attach-btn" onclick="document.getElementById('file-input').click()">📎</button>
            <input type="file" id="file-input" style="display: none;" onchange="handleFileUpload()">
            <input type="text" id="msg-input" placeholder="Message..." autocomplete="off">
            <button class="send-btn" onclick="sendMessage()">➤</button>
        </div>
    </div>

    <div id="audio-container" style="display:none;"></div>

    <script>
        // GLOBALS
        let currentUser = localStorage.getItem("kust_username");
        let userId = localStorage.getItem("kust_userid") || "user_" + Math.random().toString(36).substr(2, 9);
        localStorage.setItem("kust_userid", userId);

        let currentGroup = "General";
        let ws = null;
        let heartbeatInt = null;
        
        // Voice Globals
        let peer = null;
        let myStream = null;
        let peers = {}; 
        let isMuted = false;
        let vcUsers = new Set(); // store userIds in VC

        // --- INIT ---
        window.onload = () => {
            if (currentUser) {
                document.getElementById('login-modal').style.display = 'none';
                initApp();
            } else {
                document.getElementById('username-input').focus();
            }
        };

        function login() {
            const input = document.getElementById('username-input');
            const val = input.value.trim();
            if (!val) return;
            
            currentUser = val;
            localStorage.setItem("kust_username", val);
            document.getElementById('login-modal').style.display = 'none';
            initApp();
        }

        async function initApp() {
            await loadGroups();
            connectToGroup("General");
        }

        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('open');
        }

        async function loadGroups() {
            try {
                const res = await fetch('/api/groups');
                const data = await res.json();
                const list = document.getElementById('group-list');
                list.innerHTML = '';
                data.groups.forEach(g => {
                    const div = document.createElement('div');
                    div.className = `group-item ${g === currentGroup ? 'active' : ''}`;
                    div.innerText = g;
                    div.onclick = () => {
                        connectToGroup(g);
                        if(window.innerWidth < 768) toggleSidebar();
                    };
                    list.appendChild(div);
                });
            } catch(e) { console.error(e); }
        }

        async function createNewGroup() {
            const name = prompt("Group Name:");
            if (name) {
                await fetch('/api/groups', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: name})
                });
                loadGroups();
            }
        }

        // --- CHAT LOGIC ---

        function connectToGroup(groupName) {
            if (ws) ws.close();
            if (heartbeatInt) clearInterval(heartbeatInt);
            leaveVoice(); // Auto leave VC on switch

            currentGroup = groupName;
            document.getElementById('current-group-name').innerText = "# " + groupName;
            loadGroups(); // Update active class
            
            const list = document.getElementById('messages');
            list.innerHTML = '';

            // Load History
            fetch(`/api/history/${groupName}`)
                .then(r => r.json())
                .then(msgs => msgs.forEach(displayMessage));

            // WebSocket
            const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
            ws = new WebSocket(`${proto}://${window.location.host}/ws/${groupName}/${userId}`);

            ws.onopen = () => {
                heartbeatInt = setInterval(() => ws.send(JSON.stringify({type: "heartbeat"})), 30000);
            };

            ws.onmessage = (e) => {
                const data = JSON.parse(e.data);
                if (data.type === "heartbeat_ack") return;
                if (data.type === "message") displayMessage(data);
                if (data.type === "vc_signal") handleVcSignal(data);
                if (data.type === "vc_update") handleVcUpdate(data);
                if (data.type === "user_left") removeVcUser(data.user_id);
            };
        }

        function displayMessage(data) {
            const container = document.getElementById('messages');
            const div = document.createElement('div');
            const isMe = data.user_id === userId;
            
            div.className = `message ${isMe ? 'msg-own' : 'msg-other'}`;
            
            let content = `<span class="msg-meta">${data.user_name}</span>`;
            if (data.text) content += `<div>${data.text}</div>`;
            
            if (data.image_url) {
                // Image with loading state
                content += `
                <div class="img-container">
                    <div class="loading-spinner"></div>
                    <img src="${data.image_url}" class="msg-img" onload="this.style.display='block'; this.previousElementSibling.style.display='none';" onclick="window.open(this.src)">
                </div>`;
            }
            
            div.innerHTML = content;
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
        }

        function sendMessage() {
            const input = document.getElementById('msg-input');
            const text = input.value.trim();
            if (!text) return;

            ws.send(JSON.stringify({
                type: "message",
                user_name: currentUser,
                text: text,
                timestamp: Date.now()
            }));
            input.value = '';
        }

        document.getElementById('msg-input').addEventListener('keypress', (e) => { if(e.key==='Enter') sendMessage(); });

        async function handleFileUpload() {
            const input = document.getElementById('file-input');
            if (!input.files.length) return;
            const file = input.files[0];
            const formData = new FormData();
            formData.append('file', file);

            const status = document.getElementById('msg-input');
            const oldPlace = status.placeholder;
            status.placeholder = "Uploading...";
            status.disabled = true;

            try {
                const res = await fetch('/api/upload', {method: 'POST', body: formData});
                if(!res.ok) throw new Error("Upload Error");
                const data = await res.json();
                
                ws.send(JSON.stringify({
                    type: "message",
                    user_name: currentUser,
                    text: "",
                    image_url: data.url,
                    timestamp: Date.now()
                }));
            } catch (e) {
                alert("Upload Failed. Check connection.");
            } finally {
                input.value = '';
                status.placeholder = oldPlace;
                status.disabled = false;
                status.focus();
            }
        }

        // --- VOICE CHAT LOGIC ---

        function joinVoice() {
            navigator.mediaDevices.getUserMedia({ audio: true, video: false })
                .then(stream => {
                    myStream = stream;
                    isMuted = false;
                    
                    document.getElementById('vc-modal').style.display = 'flex';
                    
                    // Add Self to Grid
                    addVcUser(userId, currentUser, true);

                    peer = new Peer(userId);
                    
                    peer.on('open', (id) => {
                        // Broadcast Join
                        ws.send(JSON.stringify({
                            type: "vc_signal",
                            sender_name: currentUser,
                            payload: { event: "join-request" },
                            target_id: null
                        }));
                    });

                    peer.on('call', (call) => {
                        call.answer(stream);
                        const audio = document.createElement('audio');
                        call.on('stream', userStream => {
                            addAudioStream(audio, userStream);
                            // Ensure user exists in grid
                            if(!vcUsers.has(call.peer)) addVcUser(call.peer, "User", false); 
                        });
                        peers[call.peer] = call;
                    });
                })
                .catch(e => alert("Mic Access Denied: " + e));
        }

        function handleVcSignal(data) {
            if (!peer || !myStream) return;
            const senderId = data.sender_id;
            if (senderId === userId) return;

            const payload = data.payload;

            if (payload.event === "join-request") {
                // New user joined, add to UI & call them
                addVcUser(senderId, data.sender_name, false);
                
                // Call them
                const call = peer.call(senderId, myStream);
                const audio = document.createElement('audio');
                call.on('stream', us => addAudioStream(audio, us));
                peers[senderId] = call;
                
                // Tell them I am here too (so they can add me to their UI)
                ws.send(JSON.stringify({
                    type: "vc_signal",
                    sender_name: currentUser,
                    target_id: senderId,
                    payload: { event: "ack-presence" }
                }));
            } 
            else if (payload.event === "ack-presence") {
                // Existing user acknowledging my join
                addVcUser(senderId, data.sender_name, false);
            }
        }

        function handleVcUpdate(data) {
            if(!vcUsers.has(data.sender_id)) return;
            const ui = document.getElementById(`vc-u-${data.sender_id}`);
            if(ui) {
                const avatar = ui.querySelector('.vc-avatar');
                const micIcon = ui.querySelector('.vc-mic-icon');
                if(data.status.muted) {
                    avatar.classList.add('muted');
                    micIcon.style.display = 'flex';
                } else {
                    avatar.classList.remove('muted');
                    micIcon.style.display = 'none';
                }
            }
        }

        function addVcUser(uid, name, isSelf) {
            if (vcUsers.has(uid)) return;
            vcUsers.add(uid);
            
            const grid = document.getElementById('vc-grid');
            const el = document.createElement('div');
            el.className = 'vc-user';
            el.id = `vc-u-${uid}`;
            el.innerHTML = `
                <div class="vc-avatar">
                    ${name.charAt(0).toUpperCase()}
                </div>
                <div class="vc-mic-icon" style="display:none;">✕</div>
                <div class="vc-name">${name}</div>
            `;
            grid.appendChild(el);
            
            // Audio visualizer simulation
            if (!isSelf) {
                setInterval(() => {
                    const avatar = el.querySelector('.vc-avatar');
                    // Randomly glow to simulate talking if not muted
                    if (!avatar.classList.contains('muted') && Math.random() > 0.7) {
                        avatar.classList.add('speaking');
                        setTimeout(() => avatar.classList.remove('speaking'), 200);
                    }
                }, 500);
            }
        }

        function removeVcUser(uid) {
            if(vcUsers.has(uid)) {
                vcUsers.delete(uid);
                const el = document.getElementById(`vc-u-${uid}`);
                if(el) el.remove();
            }
        }

        function addAudioStream(audio, stream) {
            audio.srcObject = stream;
            audio.addEventListener('loadedmetadata', () => audio.play());
            document.getElementById('audio-container').appendChild(audio);
        }

        function toggleMute() {
            if(!myStream) return;
            isMuted = !isMuted;
            myStream.getAudioTracks()[0].enabled = !isMuted;
            
            const btn = document.getElementById('mute-btn');
            btn.classList.toggle('active');
            
            // Update UI Self
            const selfUI = document.getElementById(`vc-u-${userId}`);
            if(selfUI) {
                const av = selfUI.querySelector('.vc-avatar');
                const mic = selfUI.querySelector('.vc-mic-icon');
                if(isMuted) { av.classList.add('muted'); mic.style.display='flex'; }
                else { av.classList.remove('muted'); mic.style.display='none'; }
            }

            // Broadcast Status
            ws.send(JSON.stringify({
                type: "vc_update",
                status: { muted: isMuted }
            }));
        }

        function leaveVoice() {
            if (peer) peer.destroy();
            if (myStream) myStream.getTracks().forEach(track => track.stop());
            
            peer = null;
            myStream = null;
            peers = {};
            vcUsers.clear();
            document.getElementById('vc-grid').innerHTML = '';
            document.getElementById('audio-container').innerHTML = '';
            document.getElementById('vc-modal').style.display = 'none';
        }
    </script>
</body>
</html>
"""
