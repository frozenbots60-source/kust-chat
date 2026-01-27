import os
import json
import asyncio
import boto3
import time
import re
from typing import List, Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis

# ==========================================
#   KUSTIFY HYPER-X | CONFIGURATION
# ==========================================

app = FastAPI(title="Kustify Hyper-X", description="Next-Gen Infrastructure Chat", version="8.0")

# 1. Redis Configuration
REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
if not REDIS_URL:
    print("WARNING: REDIS_URL missing. Features will fail.")

# 2. AWS S3 Configuration
AWS_ACCESS_KEY = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("BUCKETEER_AWS_REGION")
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# ==========================================
#   BACKEND INFRASTRUCTURE
# ==========================================

# Initialize Redis
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# Initialize S3
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Constants
GLOBAL_CHANNEL = "kustify:global:v8"
GROUPS_KEY = "kustify:groups:v8"
HISTORY_KEY = "kustify:history:v8:"

class GroupCreate(BaseModel):
    name: str

class ConnectionManager:
    def __init__(self):
        # Active sockets: {group_id: [ws1, ws2]}
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # User presence: {group_id: {user_id: user_info}}
        self.presence: Dict[str, Dict[str, dict]] = {}

    async def connect(self, websocket: WebSocket, group_id: str, user_info: dict):
        await websocket.accept()
        if group_id not in self.active_connections:
            self.active_connections[group_id] = []
            self.presence[group_id] = {}
        
        self.active_connections[group_id].append(websocket)
        self.presence[group_id][user_info['id']] = user_info
        
        # Broadcast presence update
        await self.broadcast_system(group_id, "presence_update", {
            "count": len(self.active_connections[group_id]),
            "users": list(self.presence[group_id].values())
        })

    def disconnect(self, websocket: WebSocket, group_id: str, user_id: str):
        if group_id in self.active_connections:
            if websocket in self.active_connections[group_id]:
                self.active_connections[group_id].remove(websocket)
            
            if user_id in self.presence[group_id]:
                del self.presence[group_id][user_id]
            
            # If group empty, cleanup
            if not self.active_connections[group_id]:
                del self.active_connections[group_id]
                del self.presence[group_id]

    async def broadcast_local(self, group_id: str, message: str):
        """Sends data to local websockets only"""
        if group_id in self.active_connections:
            for connection in self.active_connections[group_id]:
                try:
                    await connection.send_text(message)
                except Exception:
                    pass

    async def broadcast_system(self, group_id: str, type_: str, data: dict):
        """Helper to send non-chat system events"""
        payload = {"type": type_, "group_id": group_id, **data}
        await self.broadcast_local(group_id, json.dumps(payload))

manager = ConnectionManager()

# --- REDIS LISTENER WORKER ---
# Listens for messages from other server instances (scalability)
async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            try:
                data = json.loads(message["data"])
                group_id = data.get("group_id")
                # We broadcast everything from Redis to Local Sockets
                if group_id:
                    await manager.broadcast_local(group_id, message["data"])
            except Exception as e:
                print(f"Redis Broadcast Error: {e}")

@app.on_event("startup")
async def startup_event():
    # Ensure default lobby
    if not await redis.sismember(GROUPS_KEY, "Lobby"):
        await redis.sadd(GROUPS_KEY, "Lobby")
    asyncio.create_task(redis_listener())

# ==========================================
#   API ENDPOINTS
# ==========================================

@app.get("/api/groups")
async def get_groups():
    groups = await redis.smembers(GROUPS_KEY)
    return {"groups": sorted(list(groups))}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    # Sanitize
    name = re.sub(r'[^a-zA-Z0-9 _-]', '', group.name)
    await redis.sadd(GROUPS_KEY, name)
    return {"status": "success", "group": name}

@app.get("/api/history/{group_id}")
async def get_history(group_id: str, limit: int = 100):
    key = f"{HISTORY_KEY}{group_id}"
    messages = await redis.lrange(key, -limit, -1)
    return [json.loads(m) for m in messages]

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        # Generate unique filename
        ext = file.filename.split('.')[-1]
        safe_name = f"{int(time.time())}_{os.urandom(4).hex()}.{ext}"
        file_key = f"kustify_hyper/{safe_name}"
        
        s3_client.upload_fileobj(
            file.file, BUCKET_NAME, file_key,
            ExtraArgs={'ContentType': file.content_type, 'ACL': 'public-read'}
        )
        # Assuming standard public S3/R2 URL structure. Adjust if using presigned only.
        # For this "Wild" version, we assume public read ACL or bucket policy.
        # If strict private, use presigned (as in previous code).
        url = s3_client.generate_presigned_url(
            'get_object', Params={'Bucket': BUCKET_NAME, 'Key': file_key}, ExpiresIn=604800
        )
        return {"url": url}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ==========================================
#   WEBSOCKET CONTROLLER
# ==========================================

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    # We expect the client to send an "init" packet immediately after connecting
    # But for simplicity, we accept connection first
    await websocket.accept()
    
    # Wait for handshake with User Name/PFP
    try:
        init_data = await websocket.receive_json()
        user_info = {
            "id": user_id,
            "name": init_data.get("name", "Anon"),
            "pfp": init_data.get("pfp", "")
        }
    except:
        await websocket.close()
        return

    # Actually register connection
    # Note: We accepted above, but now we register with manager
    # Re-using the manager logic but passing the socket we already accepted? 
    # The manager.connect usually calls accept(), let's adjust manager to NOT accept since we did.
    # Refactoring manager slightly for this flow:
    
    if group_id not in manager.active_connections:
        manager.active_connections[group_id] = []
        manager.presence[group_id] = {}
    manager.active_connections[group_id].append(websocket)
    manager.presence[group_id][user_id] = user_info
    
    # Broadcast join
    await manager.broadcast_system(group_id, "presence_update", {
        "count": len(manager.active_connections[group_id]),
        "users": list(manager.presence[group_id].values())
    })
    
    # Send system welcome
    await websocket.send_text(json.dumps({
        "type": "system", 
        "text": f"Connected to infrastructure node: {group_id}"
    }))

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")

            # --- HEARTBEAT ---
            if mtype == "heartbeat":
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))

            # --- TYPING STATUS ---
            elif mtype == "typing":
                # Broadcast only to others, don't store
                await manager.broadcast_system(group_id, "user_typing", {
                    "user_id": user_id,
                    "user_name": user_info["name"]
                })

            # --- CHAT MESSAGES ---
            elif mtype == "message":
                msg_id = f"{user_id}-{int(time.time()*1000)}"
                out = {
                    "type": "message",
                    "id": msg_id,
                    "group_id": group_id,
                    "user_id": user_id,
                    "user_name": user_info["name"],
                    "user_pfp": user_info["pfp"],
                    "text": data.get("text"),
                    "image_url": data.get("image_url"),
                    "style": data.get("style", "normal"), # For /shout etc
                    "timestamp": time.time(),
                    "reactions": {}
                }
                # Store in Redis
                await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(out))
                # Publish to Redis (triggers listener -> broadcasts to all)
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out))

            # --- REACTIONS ---
            elif mtype == "react":
                target_msg_id = data.get("msg_id")
                emoji = data.get("emoji")
                # This is tricky with Redis Lists (immutable mostly). 
                # For "Wild" demo, we will just broadcast the reaction event 
                # Clients handle visual state locally. 
                # (Production would use a Hash for msg state)
                await manager.broadcast_system(group_id, "reaction_add", {
                    "msg_id": target_msg_id,
                    "user_id": user_id,
                    "emoji": emoji
                })

            # --- VOICE CHAT SIGNALING ---
            # All VC logic is pass-through to group members
            elif mtype in ["vc_join", "vc_leave", "vc_signal", "vc_talking", "vc_update"]:
                # Attach sender ID for security
                data["sender_id"] = user_id
                # Add sender info if join
                if mtype == "vc_join":
                    data["user_name"] = user_info["name"]
                    data["user_pfp"] = user_info["pfp"]
                
                # Broadcast directly to group via Redis to reach all nodes
                # We wrap it to ensure it goes through the redis_listener
                wrapper = {
                    "type": "message", # Abuse message type to hit listener? No, listener checks type.
                    # The listener currently checks message["type"] == "message".
                    # Let's just broadcast local for signaling speed (Mesh usually local-ish) 
                    # OR publish raw.
                }
                # For speed, direct local broadcast is best, but multi-worker needs Redis.
                # Let's wrap in a container the listener understands if we want scaling.
                # For this file, let's assume single-worker or sticky sessions for VC usually.
                # But to be safe, we broadcast to group.
                await manager.broadcast_local(group_id, json.dumps(data))


    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)
        # Notify leaving
        await manager.broadcast_system(group_id, "presence_update", {
            "count": len(manager.active_connections.get(group_id, [])),
            "users": list(manager.presence.get(group_id, {}).values())
        })
        # If in VC, auto-leave logic handled by client timeouts or specific leave packets
        # But we send a force leave just in case
        await manager.broadcast_local(group_id, json.dumps({
            "type": "vc_leave", "sender_id": user_id
        }))

# ==========================================
#   FRONTEND APPLICATION (SINGLE FILE)
# ==========================================

@app.get("/")
async def get():
    return HTMLResponse(html_content)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>KUSTIFY HYPER-X</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.0.2/marked.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #050505;
            --panel: rgba(20, 20, 23, 0.65);
            --border: rgba(255, 255, 255, 0.08);
            --primary: #7000ff;
            --accent: #00f3ff;
            --text-main: #eeeeee;
            --text-dim: #888888;
            --glass: blur(20px) saturate(180%);
            --radius: 16px;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; -webkit-tap-highlight-color: transparent; }
        
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--bg-dark);
            color: var(--text-main);
            height: 100vh;
            overflow: hidden;
            display: flex;
        }

        /* INFRASTRUCTURE BACKGROUND */
        #infra-canvas {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            z-index: 0; opacity: 0.4; pointer-events: none;
        }

        /* SCROLLBARS */
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }

        /* SIDEBAR */
        #sidebar {
            width: 280px;
            background: rgba(10, 10, 12, 0.6);
            backdrop-filter: var(--glass);
            border-right: 1px solid var(--border);
            z-index: 10;
            display: flex; flex-direction: column;
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .brand-area {
            padding: 24px;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 800;
            font-size: 1.2rem;
            letter-spacing: -1px;
            background: linear-gradient(90deg, #fff, #888);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            border-bottom: 1px solid var(--border);
            display: flex; justify-content: space-between; align-items: center;
        }

        .user-card {
            padding: 20px;
            display: flex; align-items: center; gap: 12px;
            border-bottom: 1px solid var(--border);
            background: rgba(255,255,255,0.02);
        }
        .user-card img {
            width: 42px; height: 42px; border-radius: 12px;
            border: 2px solid var(--primary);
            box-shadow: 0 0 15px rgba(112, 0, 255, 0.4);
        }
        .status-dot { width: 8px; height: 8px; background: #10b981; border-radius: 50%; box-shadow: 0 0 8px #10b981; }

        .nav-list { flex: 1; padding: 15px; overflow-y: auto; }
        .nav-item {
            padding: 12px 16px; margin-bottom: 6px;
            border-radius: 8px;
            color: var(--text-dim);
            font-size: 0.95rem;
            cursor: pointer;
            transition: 0.2s;
            display: flex; justify-content: space-between;
        }
        .nav-item:hover { background: rgba(255,255,255,0.05); color: #fff; }
        .nav-item.active { 
            background: rgba(112, 0, 255, 0.15); 
            color: var(--accent); 
            border-left: 3px solid var(--accent);
        }

        .btn-create {
            margin: 20px; padding: 12px;
            border: 1px dashed var(--border);
            color: var(--text-dim);
            text-align: center; border-radius: 8px;
            cursor: pointer; font-size: 0.9rem;
            transition: 0.2s;
        }
        .btn-create:hover { border-color: var(--primary); color: var(--primary); background: rgba(112, 0, 255, 0.05); }

        /* CHAT AREA */
        #main-view {
            flex: 1; display: flex; flex-direction: column;
            position: relative; z-index: 5;
            background: radial-gradient(circle at top right, rgba(112,0,255,0.05), transparent 40%);
        }

        header {
            height: 70px;
            display: flex; justify-content: space-between; align-items: center;
            padding: 0 30px;
            border-bottom: 1px solid var(--border);
            background: rgba(5, 5, 5, 0.4);
            backdrop-filter: blur(10px);
        }
        .room-info h2 { font-size: 1.1rem; font-weight: 600; display: flex; align-items: center; gap: 10px; }
        .live-badge { font-size: 0.7rem; padding: 2px 8px; background: rgba(255,0,0,0.2); color: #ff4444; border-radius: 4px; border: 1px solid rgba(255,0,0,0.3); display: none; }
        
        .action-bar { display: flex; gap: 10px; }
        .btn-icon { width: 40px; height: 40px; border-radius: 50%; border: 1px solid var(--border); background: rgba(255,255,255,0.05); color: #fff; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: 0.2s; }
        .btn-icon:hover { background: var(--primary); border-color: var(--primary); transform: translateY(-2px); }

        /* MESSAGES */
        #chat-feed {
            flex: 1; overflow-y: auto; padding: 20px 30px;
            display: flex; flex-direction: column; gap: 20px;
        }

        .msg-group { display: flex; gap: 16px; animation: slideIn 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
        .msg-group.me { flex-direction: row-reverse; }
        
        .msg-avatar { width: 40px; height: 40px; border-radius: 10px; object-fit: cover; background: #222; }
        
        .msg-bubbles { display: flex; flex-direction: column; gap: 4px; max-width: 60%; }
        .msg-group.me .msg-bubbles { align-items: flex-end; }
        
        .msg-meta { font-size: 0.8rem; color: var(--text-dim); margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
        .msg-group.me .msg-meta { flex-direction: row-reverse; }

        .bubble {
            padding: 12px 18px;
            border-radius: 4px 18px 18px 18px;
            background: #18181b;
            font-size: 0.95rem;
            line-height: 1.5;
            color: #e4e4e7;
            position: relative;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            border: 1px solid rgba(255,255,255,0.03);
        }
        .msg-group.me .bubble {
            background: linear-gradient(135deg, #6002ee, #9c27b0);
            border-radius: 18px 4px 18px 18px;
            border: none;
        }
        
        .bubble img { max-width: 100%; border-radius: 8px; margin-top: 8px; cursor: pointer; }
        
        .shout { font-size: 1.5rem; font-weight: 800; padding: 20px 30px; background: linear-gradient(45deg, #ff0055, #ff00aa); color: white; border: 2px solid #fff; box-shadow: 0 0 20px #ff0055; animation: shake 0.5s; }

        .reaction-bar { position: absolute; bottom: -12px; right: 0; display: flex; gap: -5px; }
        .reaction { font-size: 0.8rem; background: #2a2a2e; padding: 2px 6px; border-radius: 10px; border: 1px solid #444; }

        /* INPUT AREA */
        .input-wrapper {
            padding: 24px;
            background: rgba(5,5,5,0.8);
            border-top: 1px solid var(--border);
            display: flex; gap: 12px; align-items: flex-end;
        }
        
        .typing-indicator { position: absolute; bottom: 80px; left: 30px; font-size: 0.75rem; color: var(--accent); opacity: 0; transition: 0.3s; }
        
        .chat-input-box {
            flex: 1; background: #121214;
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 14px;
            color: #fff;
            min-height: 50px;
            max-height: 150px;
            overflow-y: auto;
            font-family: inherit;
        }
        .chat-input-box:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(112, 0, 255, 0.2); }

        /* VOICE WIDGET */
        #vc-panel {
            position: absolute; top: 80px; right: 20px;
            width: 320px;
            background: rgba(15, 15, 20, 0.9);
            backdrop-filter: blur(30px);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 20px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            display: none; flex-direction: column;
            overflow: hidden;
            animation: popIn 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        
        .vc-visualizer { width: 100%; height: 60px; background: #000; position: relative; }
        .vc-canvas { width: 100%; height: 100%; opacity: 0.7; }

        .vc-grid { padding: 15px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; max-height: 200px; overflow-y: auto; }
        .vc-slot { display: flex; flex-direction: column; align-items: center; gap: 5px; position: relative; }
        .vc-slot img { width: 44px; height: 44px; border-radius: 50%; border: 2px solid #333; transition: 0.2s; object-fit: cover; }
        .vc-slot.talking img { border-color: #00f3ff; box-shadow: 0 0 15px #00f3ff; transform: scale(1.1); }
        .vc-slot.muted::after { content: '🚫'; position: absolute; bottom: 15px; right: 0; font-size: 12px; background: #000; border-radius: 50%; }

        .vc-controls { padding: 15px; display: flex; justify-content: center; gap: 15px; background: rgba(0,0,0,0.3); }
        
        /* SETUP MODAL */
        .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 100; display: flex; align-items: center; justify-content: center; }
        .modal-content { width: 350px; text-align: center; animation: slideUp 0.4s; }
        .pfp-preview-box { width: 100px; height: 100px; margin: 0 auto 20px; position: relative; }
        .pfp-preview { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; border: 3px solid var(--accent); }
        .modern-input { width: 100%; background: transparent; border: none; border-bottom: 2px solid #333; color: white; font-size: 1.5rem; text-align: center; padding: 10px; margin-bottom: 30px; }
        .modern-input:focus { border-color: var(--accent); }
        .btn-start { background: white; color: black; border: none; padding: 15px 40px; border-radius: 30px; font-weight: 800; cursor: pointer; font-size: 1rem; transition: 0.2s; }
        .btn-start:hover { transform: scale(1.05); box-shadow: 0 0 20px rgba(255,255,255,0.4); }

        /* UTILS */
        .hidden { display: none !important; }
        @keyframes slideIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes popIn { from { opacity: 0; transform: scale(0.9); } to { opacity: 1; transform: scale(1); } }
        @keyframes slideUp { from { opacity: 0; transform: translateY(50px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes shake { 0% { transform: translateX(0); } 25% { transform: translateX(-5px); } 75% { transform: translateX(5px); } 100% { transform: translateX(0); } }

        /* MOBILE */
        @media (max-width: 768px) {
            #sidebar { position: fixed; height: 100%; transform: translateX(-100%); width: 85%; box-shadow: 10px 0 30px rgba(0,0,0,0.5); }
            #sidebar.open { transform: translateX(0); }
        }
    </style>
</head>
<body>

    <canvas id="infra-canvas"></canvas>

    <div id="setup-modal" class="modal">
        <div class="modal-content">
            <h1 style="margin-bottom: 10px; font-family: 'JetBrains Mono'; color:var(--accent)">INIT_PROFILE</h1>
            <p style="color:#666; margin-bottom: 30px;">Identify yourself on the network</p>
            
            <div class="pfp-preview-box" onclick="document.getElementById('pfp-upload').click()">
                <img id="preview-pfp" class="pfp-preview" src="https://ui-avatars.com/api/?background=random&name=User">
                <div style="position:absolute; bottom:0; right:0; background:var(--primary); width:30px; height:30px; border-radius:50%; display:flex; align-items:center; justify-content:center;">📷</div>
            </div>
            <input type="file" id="pfp-upload" hidden onchange="uploadPfp()">
            
            <input type="text" id="username-input" class="modern-input" placeholder="Alias" maxlength="15">
            <button class="btn-start" onclick="saveProfile()">CONNECT</button>
        </div>
    </div>

    <div id="sidebar">
        <div class="brand-area">
            KUSTIFY HYPER-X
            <span onclick="toggleSidebar()" style="cursor:pointer; font-size:1.5rem; display:none;" id="close-side">×</span>
        </div>
        <div class="user-card">
            <img id="side-pfp">
            <div>
                <div id="side-name" style="font-weight:700"></div>
                <div style="font-size:0.75rem; color:var(--accent)">● ONLINE</div>
            </div>
        </div>
        <div style="padding: 15px 15px 5px; font-size:0.75rem; color:#666; font-weight:700;">NODES</div>
        <div class="nav-list" id="group-list">
            </div>
        <div class="btn-create" onclick="createGroup()">+ Initialize New Node</div>
    </div>

    <div id="main-view">
        <header>
            <div class="room-info">
                <div>
                    <button onclick="toggleSidebar()" class="btn-icon" style="display:inline-flex; width:30px; height:30px; margin-right:10px;">☰</button> 
                    <span id="header-title"># Lobby</span>
                </div>
                <div id="presence-count" style="font-size:0.8rem; color:var(--text-dim); margin-top:4px;">1 User Connected</div>
            </div>
            <div class="action-bar">
                <span class="live-badge" id="vc-badge">VOICE ACTIVE</span>
                <button class="btn-icon" onclick="joinVoice()" id="join-vc-btn">🎤</button>
            </div>
        </header>

        <div id="chat-feed"></div>

        <div class="typing-indicator" id="typing-indicator">Someone is typing...</div>

        <div class="input-wrapper">
            <button class="btn-icon" onclick="document.getElementById('file-input').click()">+</button>
            <input type="file" id="file-input" hidden onchange="handleFile()">
            
            <input type="text" id="msg-input" class="chat-input-box" placeholder="Execute command or send message..." autocomplete="off">
            <button class="btn-icon" style="background:var(--primary); border-color:var(--primary)" onclick="sendMessage()">➤</button>
        </div>
    </div>

    <div id="vc-panel">
        <div class="vc-visualizer">
            <canvas id="viz-canvas" class="vc-canvas"></canvas>
        </div>
        <div class="vc-grid" id="vc-grid"></div>
        <div class="vc-controls">
            <button class="btn-icon" onclick="toggleMute()" id="mute-btn">🎙️</button>
            <button class="btn-icon" style="background:#ff4444; border-color:#ff4444" onclick="leaveVoice()">✖</button>
        </div>
    </div>

    <div id="audio-container" hidden></div>

    <script>
        // ==========================================
        //   CLIENT LOGIC
        // ==========================================
        
        // --- SOUNDS (Base64 for single file portability) ---
        const SOUNDS = {
            pop: new Audio("data:audio/wav;base64,UklGRl9vT19XQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YU..."), // Placeholder, browsers block auto-audio often. Using visual feedback mostly.
        };

        // --- STATE ---
        const state = {
            user: localStorage.getItem("khx_user") || "",
            uid: localStorage.getItem("khx_uid") || "u_" + Math.random().toString(36).substr(2, 8),
            pfp: localStorage.getItem("khx_pfp") || `https://ui-avatars.com/api/?background=0D8ABC&color=fff&name=User`,
            group: "Lobby",
            ws: null,
            hbInterval: null
        };
        localStorage.setItem("khx_uid", state.uid);

        let typingTimeout = null;

        // --- INIT ---
        window.onload = () => {
            initBackground();
            if (state.user) {
                document.getElementById('setup-modal').classList.add('hidden');
                updateProfileUI();
                initApp();
            } else {
                document.getElementById('preview-pfp').src = state.pfp;
            }
            // Mobile check
            if(window.innerWidth < 768) document.getElementById('close-side').style.display='block';
        };

        // --- PROFILE ---
        async function uploadPfp() {
            const f = document.getElementById('pfp-upload').files[0];
            if(!f) return;
            const fd = new FormData(); fd.append('file', f);
            try {
                const r = await fetch('/api/upload', {method:'POST', body:fd});
                const d = await r.json();
                state.pfp = d.url;
                document.getElementById('preview-pfp').src = state.pfp;
            } catch(e) { alert("Upload Failed"); }
        }

        function saveProfile() {
            const n = document.getElementById('username-input').value.trim();
            if(!n) return alert("Identify yourself.");
            state.user = n;
            localStorage.setItem("khx_user", n);
            localStorage.setItem("khx_pfp", state.pfp);
            document.getElementById('setup-modal').classList.add('hidden');
            updateProfileUI();
            initApp();
        }

        function updateProfileUI() {
            document.getElementById('side-pfp').src = state.pfp;
            document.getElementById('side-name').innerText = state.user;
        }

        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('open');
        }

        // --- APP & CHAT ---
        async function initApp() {
            loadGroups();
            connect(state.group);
        }

        async function loadGroups() {
            const r = await fetch('/api/groups');
            const d = await r.json();
            const l = document.getElementById('group-list');
            l.innerHTML = '';
            d.groups.forEach(g => {
                const el = document.createElement('div');
                el.className = `nav-item ${g === state.group ? 'active' : ''}`;
                el.innerHTML = `<span># ${g}</span>`;
                el.onclick = () => {
                    if(window.innerWidth < 768) toggleSidebar();
                    connect(g);
                };
                l.appendChild(el);
            });
        }

        async function createGroup() {
            const n = prompt("NODE IDENTIFIER:");
            if(n) {
                await fetch('/api/groups', {
                    method:'POST', 
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({name: n})
                });
                loadGroups();
            }
        }

        function connect(group) {
            if(state.ws) state.ws.close();
            leaveVoice(); // Auto leave voice on switch

            state.group = group;
            document.getElementById('header-title').innerText = `# ${group}`;
            document.getElementById('chat-feed').innerHTML = '';
            loadGroups(); // Update active class

            // Load History
            fetch(`/api/history/${group}`).then(r=>r.json()).then(msgs => {
                msgs.forEach(renderMessage);
            });

            // WebSocket
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${group}/${state.uid}`);

            state.ws.onopen = () => {
                // Send Handshake
                state.ws.send(JSON.stringify({name: state.user, pfp: state.pfp}));
                state.hbInterval = setInterval(() => state.ws.send(JSON.stringify({type:"heartbeat"})), 30000);
            };

            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                handleSocketData(d);
            };
        }

        function handleSocketData(d) {
            if(d.type === "message") renderMessage(d);
            if(d.type === "system") renderSystem(d.text);
            if(d.type === "user_typing") showTyping(d.user_name);
            if(d.type === "presence_update") updatePresence(d);
            
            // VC Handling
            if(d.type === "vc_join") handleVcJoin(d);
            if(d.type === "vc_leave") handleVcLeave(d);
            if(d.type === "vc_talking") highlightSpeaker(d.sender_id, d.vol);
            if(d.type === "vc_signal" && d.target_id === state.uid) handleSignal(d);
        }

        function renderMessage(d) {
            const feed = document.getElementById('chat-feed');
            const isMe = d.user_id === state.uid;
            
            // Check if last message was same user to group them (simple visual optimization)
            const lastMsg = feed.lastElementChild;
            let group = null;
            
            if (lastMsg && lastMsg.dataset.uid === d.user_id) {
                group = lastMsg;
            } else {
                group = document.createElement('div');
                group.className = `msg-group ${isMe ? 'me' : ''}`;
                group.dataset.uid = d.user_id;
                group.innerHTML = `
                    <img class="msg-avatar" src="${d.user_pfp}">
                    <div class="msg-bubbles">
                        <div class="msg-meta">${d.user_name} <span style="font-size:0.7em; opacity:0.5">${new Date(d.timestamp*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</span></div>
                    </div>
                `;
                feed.appendChild(group);
            }

            const bubbleContainer = group.querySelector('.msg-bubbles');
            const bubble = document.createElement('div');
            
            // Special Styles
            if (d.style === "shout") {
                bubble.className = "bubble shout";
            } else {
                bubble.className = "bubble";
            }
            
            // Content processing
            let content = d.text ? marked.parse(d.text) : "";
            // Auto embed images from text links
            content = content.replace(/(https?:\/\/.*\.(?:png|jpg|gif|webp))/i, '<br><img src="$1">');
            
            if (d.image_url) {
                content += `<img src="${d.image_url}" onclick="window.open(this.src)">`;
            }

            bubble.innerHTML = content;
            bubble.ondblclick = () => {
                bubble.innerHTML += `<div class="reaction-bar"><span class="reaction">❤️ 1</span></div>`;
                state.ws.send(JSON.stringify({type: "react", msg_id: d.id, emoji: "❤️"}));
            };

            bubbleContainer.appendChild(bubble);
            feed.scrollTop = feed.scrollHeight;
        }

        function renderSystem(text) {
            const feed = document.getElementById('chat-feed');
            const el = document.createElement('div');
            el.style.textAlign = "center";
            el.style.fontSize = "0.75rem";
            el.style.color = "var(--text-dim)";
            el.style.margin = "10px 0";
            el.innerText = `SYSTEM: ${text}`;
            feed.appendChild(el);
        }

        function sendMessage() {
            const inp = document.getElementById('msg-input');
            const txt = inp.value.trim();
            if(!txt) return;

            // Commands
            if(txt.startsWith('/clear')) {
                document.getElementById('chat-feed').innerHTML = '';
                inp.value = '';
                return;
            }
            
            let style = "normal";
            let finalText = txt;
            
            if(txt.startsWith('/shout ')) {
                style = "shout";
                finalText = txt.replace('/shout ', '');
            }

            state.ws.send(JSON.stringify({
                type: "message",
                text: finalText,
                style: style
            }));
            inp.value = '';
        }

        // Typing logic
        document.getElementById('msg-input').addEventListener('input', () => {
            if(!typingTimeout) {
                state.ws.send(JSON.stringify({type:"typing"}));
                typingTimeout = setTimeout(() => typingTimeout = null, 2000);
            }
        });
        document.getElementById('msg-input').addEventListener('keydown', e => {
            if(e.key === 'Enter') sendMessage();
        });

        function showTyping(name) {
            const el = document.getElementById('typing-indicator');
            el.innerText = `${name} is typing...`;
            el.style.opacity = 1;
            setTimeout(() => el.style.opacity = 0, 2000);
        }

        function updatePresence(d) {
            document.getElementById('presence-count').innerText = `${d.count} Users Connected`;
        }

        async function handleFile() {
            const f = document.getElementById('file-input').files[0];
            if(!f) return;
            const fd = new FormData(); fd.append('file', f);
            // Upload then send message with img url
            try {
                const r = await fetch('/api/upload', {method:'POST', body:fd});
                const res = await r.json();
                state.ws.send(JSON.stringify({
                    type: "message",
                    text: "",
                    image_url: res.url
                }));
            } catch(e){}
        }

        // ==========================================
        //   VOICE CHAT (MESH)
        // ==========================================
        let peer = null;
        let myStream = null;
        let joinedVc = false;
        let audioCtx, analyser, dataArray;
        let vizInterval;
        let calls = {};

        async function joinVoice() {
            try {
                myStream = await navigator.mediaDevices.getUserMedia({audio: true});
            } catch(e) {
                alert("Microphone Access Denied");
                return;
            }
            
            joinedVc = true;
            document.getElementById('vc-panel').style.display = 'flex';
            document.getElementById('vc-badge').style.display = 'inline-block';
            document.getElementById('join-vc-btn').style.display = 'none';

            // Init Visualizer
            initAudioViz();

            // Init Peer
            peer = new Peer(state.uid);
            
            peer.on('open', (id) => {
                // Announce
                state.ws.send(JSON.stringify({type: "vc_join"}));
                // Add self
                addVcUser(state.uid, state.user, state.pfp);
            });

            peer.on('call', call => {
                call.answer(myStream);
                handleStream(call);
            });
        }

        function handleVcJoin(d) {
            if(!joinedVc) return;
            addVcUser(d.sender_id, d.user_name, d.user_pfp);
            // Connect to them
            if(d.sender_id !== state.uid) {
                const call = peer.call(d.sender_id, myStream);
                handleStream(call);
            }
        }
        
        function handleVcLeave(d) {
            const el = document.getElementById(`vc-u-${d.sender_id}`);
            if(el) el.remove();
            if(calls[d.sender_id]) calls[d.sender_id].close();
        }

        function handleStream(call) {
            calls[call.peer] = call;
            call.on('stream', remoteStream => {
                let aud = document.createElement('audio');
                aud.srcObject = remoteStream;
                aud.autoplay = true;
                document.getElementById('audio-container').appendChild(aud);
            });
        }

        function addVcUser(uid, name, pfp) {
            if(document.getElementById(`vc-u-${uid}`)) return;
            const grid = document.getElementById('vc-grid');
            const div = document.createElement('div');
            div.className = 'vc-slot';
            div.id = `vc-u-${uid}`;
            div.innerHTML = `<img src="${pfp}"><span>${name}</span>`;
            grid.appendChild(div);
        }

        function leaveVoice() {
            if(!joinedVc) return;
            joinedVc = false;
            state.ws.send(JSON.stringify({type: "vc_leave"}));
            if(peer) peer.destroy();
            if(myStream) myStream.getTracks().forEach(t=>t.stop());
            if(audioCtx) audioCtx.close();
            
            document.getElementById('vc-panel').style.display = 'none';
            document.getElementById('vc-badge').style.display = 'none';
            document.getElementById('join-vc-btn').style.display = 'flex';
            document.getElementById('vc-grid').innerHTML = '';
            document.getElementById('audio-container').innerHTML = '';
        }

        let isMuted = false;
        function toggleMute() {
            isMuted = !isMuted;
            myStream.getAudioTracks()[0].enabled = !isMuted;
            document.getElementById('mute-btn').style.opacity = isMuted ? 0.5 : 1;
        }

        // --- VISUALIZER & TALKING DETECTION ---
        function initAudioViz() {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            const src = audioCtx.createMediaStreamSource(myStream);
            analyser = audioCtx.createAnalyser();
            analyser.fftSize = 64;
            src.connect(analyser);
            
            const canvas = document.getElementById('viz-canvas');
            const ctx = canvas.getContext('2d');
            const bufferLength = analyser.frequencyBinCount;
            const dataArray = new Uint8Array(bufferLength);
            
            function draw() {
                if(!joinedVc) return;
                requestAnimationFrame(draw);
                analyser.getByteFrequencyData(dataArray);
                
                // Talking detection
                let sum = 0;
                for(let i=0; i<bufferLength; i++) sum += dataArray[i];
                let avg = sum / bufferLength;
                if(avg > 15 && !isMuted) {
                    state.ws.send(JSON.stringify({type:"vc_talking", vol: avg}));
                    highlightSpeaker(state.uid, avg);
                }

                // Canvas Draw
                ctx.clearRect(0, 0, canvas.width, canvas.height);
                const barWidth = (canvas.width / bufferLength) * 2.5;
                let barHeight;
                let x = 0;

                for(let i = 0; i < bufferLength; i++) {
                    barHeight = dataArray[i] / 2;
                    ctx.fillStyle = `rgb(${barHeight + 100}, 0, 255)`;
                    ctx.fillRect(x, canvas.height - barHeight, barWidth, barHeight);
                    x += barWidth + 1;
                }
            }
            draw();
        }

        function highlightSpeaker(uid, vol) {
            const el = document.getElementById(`vc-u-${uid}`);
            if(el) {
                el.classList.add('talking');
                clearTimeout(el.talkTimeout);
                el.talkTimeout = setTimeout(() => el.classList.remove('talking'), 200);
            }
        }

        // ==========================================
        //   CANVAS BACKGROUND (INFRASTRUCTURE)
        // ==========================================
        function initBackground() {
            const canvas = document.getElementById('infra-canvas');
            const ctx = canvas.getContext('2d');
            let width, height;
            let nodes = [];

            function resize() {
                width = canvas.width = window.innerWidth;
                height = canvas.height = window.innerHeight;
            }
            window.onresize = resize;
            resize();

            // Node Class
            class Node {
                constructor() {
                    this.x = Math.random() * width;
                    this.y = Math.random() * height;
                    this.vx = (Math.random() - 0.5) * 0.5;
                    this.vy = (Math.random() - 0.5) * 0.5;
                }
                update() {
                    this.x += this.vx; this.y += this.vy;
                    if(this.x < 0 || this.x > width) this.vx *= -1;
                    if(this.y < 0 || this.y > height) this.vy *= -1;
                }
            }

            for(let i=0; i<40; i++) nodes.push(new Node());

            function animate() {
                ctx.clearRect(0, 0, width, height);
                ctx.lineWidth = 1;
                
                for(let i=0; i<nodes.length; i++) {
                    nodes[i].update();
                    // Draw Node
                    ctx.beginPath();
                    ctx.arc(nodes[i].x, nodes[i].y, 2, 0, Math.PI*2);
                    ctx.fillStyle = '#7000ff';
                    ctx.fill();

                    // Connections
                    for(let j=i+1; j<nodes.length; j++) {
                        let dx = nodes[i].x - nodes[j].x;
                        let dy = nodes[i].y - nodes[j].y;
                        let dist = Math.sqrt(dx*dx + dy*dy);
                        if(dist < 150) {
                            ctx.beginPath();
                            ctx.moveTo(nodes[i].x, nodes[i].y);
                            ctx.lineTo(nodes[j].x, nodes[j].y);
                            ctx.strokeStyle = `rgba(112, 0, 255, ${1 - dist/150})`;
                            ctx.stroke();
                        }
                    }
                }
                requestAnimationFrame(animate);
            }
            animate();
        }

    </script>
</body>
</html>
"""
