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
from botocore.config import Config

# ==========================================
# KUSTIFY HYPER-X | V9.0 (STABLE - FIXED)
# ==========================================

app = FastAPI(title="Kustify Hyper-X", description="Next-Gen Infrastructure Chat", version="9.0")

# 1. Redis Configuration
RAW_REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("REDIS_URL")
REDIS_URL = RAW_REDIS_URL

# 2. AWS S3 Configuration
AWS_ACCESS_KEY = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("BUCKETEER_AWS_REGION")
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# ==========================================
# BACKEND INFRASTRUCTURE
# ==========================================

# Initialize Redis
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# Initialize S3 with SigV4 support
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
    config=Config(signature_version='s3v4')
)

# Constants
GLOBAL_CHANNEL = "kustify:global:v9"
GROUPS_KEY = "kustify:groups:v9"
HISTORY_KEY = "kustify:history:v9:"
USERNAMES_KEY = "kustify:usernames:v9"

class GroupCreate(BaseModel):
    name: str

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
        self.global_lookup: Dict[str, WebSocket] = {}
        self.user_meta: Dict[str, dict] = {}

    async def connect(self, websocket: WebSocket, group_id: str, user_info: dict):
        uid = user_info['id']
        if group_id not in self.active_connections:
            self.active_connections[group_id] = []
        self.active_connections[group_id].append(websocket)
        self.global_lookup[uid] = websocket
        self.user_meta[uid] = {**user_info, "group": group_id}
        await self.broadcast_presence(group_id)

    def disconnect(self, websocket: WebSocket, group_id: str, user_id: str):
        if group_id in self.active_connections:
            if websocket in self.active_connections[group_id]:
                self.active_connections[group_id].remove(websocket)
                if not self.active_connections[group_id]:
                    del self.active_connections[group_id]
        if user_id in self.global_lookup:
            del self.global_lookup[user_id]
        if user_id in self.user_meta:
            del self.user_meta[user_id]
        return group_id

    async def broadcast_presence(self, group_id: str):
        if group_id not in self.active_connections:
            return
        users_in_group = [
            meta for uid, meta in self.user_meta.items() 
            if meta.get("group") == group_id
        ]
        payload = json.dumps({
            "type": "presence_update",
            "group_id": group_id,
            "count": len(users_in_group),
            "users": users_in_group
        })
        await self.broadcast_local(group_id, payload)

    async def broadcast_local(self, group_id: str, message: str):
        if group_id in self.active_connections:
            for connection in self.active_connections[group_id]:
                try:
                    await connection.send_text(message)
                except Exception:
                    pass

    async def send_personal_message(self, target_id: str, message: str):
        if target_id in self.global_lookup:
            try:
                await self.global_lookup[target_id].send_text(message)
                return True
            except:
                return False
        return False

manager = ConnectionManager()

# --- REDIS LISTENER WORKER ---
async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            try:
                data = json.loads(message["data"])
                mtype = data.get("type")
                if mtype in ["message", "edit_message", "delete_message", "vc_signal_group"]:
                    group_id = data.get("group_id")
                    if group_id:
                        await manager.broadcast_local(group_id, message["data"])
                elif mtype == "dm":
                    target_id = data.get("target_id")
                    if target_id:
                        await manager.send_personal_message(target_id, message["data"])
            except Exception as e:
                print(f"Redis Listener Error: {e}")

@app.on_event("startup")
async def startup_event():
    try:
        if not await redis.sismember(GROUPS_KEY, "Lobby"):
            await redis.sadd(GROUPS_KEY, "Lobby")
        asyncio.create_task(redis_listener())
    except Exception as e:
        print(f"CRITICAL: Startup Redis Failure: {e}")

# ==========================================
# API ENDPOINTS
# ==========================================

@app.get("/api/groups")
async def get_groups():
    groups = await redis.smembers(GROUPS_KEY)
    return {"groups": sorted(list(groups))}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    name = re.sub(r'[^a-zA-Z0-9 _-]', '', group.name)
    if not name: return JSONResponse(status_code=400, content={"error": "Invalid name"})
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
        ext = file.filename.split('.')[-1]
        safe_name = f"{int(time.time())}_{os.urandom(4).hex()}.{ext}"
        file_key = f"kustify_v9/{safe_name}"
        s3_client.upload_fileobj(
            file.file, BUCKET_NAME, file_key,
            ExtraArgs={'ContentType': file.content_type}
        )
        url = s3_client.generate_presigned_url(
            'get_object', Params={'Bucket': BUCKET_NAME, 'Key': file_key}, ExpiresIn=604800
        )
        return {"url": url}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ==========================================
# WEBSOCKET CONTROLLER
# ==========================================

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    
    try:
        init_data = await websocket.receive_json()
        name = init_data.get("name", "Anon").strip()
        pfp = init_data.get("pfp", "")
        
        is_new = await redis.sadd(USERNAMES_KEY, name)
        if is_new == 0:
            # Check if it's the same user reconnecting
            pass 
            
        user_info = {"id": user_id, "name": name, "pfp": pfp}
    except Exception:
        await websocket.close()
        return

    await manager.connect(websocket, group_id, user_info)
    
    await websocket.send_text(json.dumps({
        "type": "system", 
        "text": f"Secure Connection Established: {group_id.upper()}"
    }))

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")

            if mtype == "heartbeat":
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))

            elif mtype == "message":
                msg_id = f"m-{int(time.time()*1000)}"
                out = {
                    "type": "message",
                    "id": msg_id,
                    "group_id": group_id,
                    "user_id": user_id,
                    "user_name": name,
                    "user_pfp": pfp,
                    "text": data.get("text"),
                    "image_url": data.get("image_url"),
                    "style": data.get("style", "normal"),
                    "timestamp": time.time(),
                    "edited": False
                }
                await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(out))
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out))

            elif mtype == "edit_message":
                msg_id = data.get("message_id")
                new_text = data.get("text")
                out = {
                    "type": "edit_message",
                    "id": msg_id,
                    "group_id": group_id,
                    "text": new_text
                }
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out))

            elif mtype == "delete_message":
                msg_id = data.get("message_id")
                out = {
                    "type": "delete_message",
                    "id": msg_id,
                    "group_id": group_id
                }
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out))

            elif mtype == "dm":
                target_id = data.get("target_id")
                if target_id:
                    out = {
                        "type": "dm",
                        "sender_id": user_id,
                        "sender_name": name,
                        "sender_pfp": pfp,
                        "target_id": target_id,
                        "text": data.get("text"),
                        "timestamp": time.time()
                    }
                    await redis.publish(GLOBAL_CHANNEL, json.dumps(out))
                    await websocket.send_text(json.dumps(out))

            elif mtype in ["vc_join", "vc_leave", "vc_signal", "vc_talking"]:
                data["sender_id"] = user_id
                if mtype == "vc_join":
                    data["user_name"] = name
                    data["user_pfp"] = pfp
                data["group_id"] = group_id
                
                if mtype == "vc_signal":
                    target = data.get("target_id")
                    if target:
                        payload = json.dumps({"type": "dm", "target_id": target, **data})
                        await redis.publish(GLOBAL_CHANNEL, payload)
                else:
                    data["type"] = "vc_signal_group" 
                    await redis.publish(GLOBAL_CHANNEL, json.dumps(data))

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)
        await redis.srem(USERNAMES_KEY, name)
        await manager.broadcast_presence(group_id)
        await redis.publish(GLOBAL_CHANNEL, json.dumps({
            "type": "vc_signal_group",
            "group_id": group_id,
            "sender_id": user_id,
            "subtype": "vc_leave"
        }))

# ==========================================
# FRONTEND APPLICATION
# ==========================================

@app.get("/")
async def get():
    return HTMLResponse(html_content)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>KUSTIFY HYPER-X</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.0.2/marked.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0e1621;
            --sidebar: #17212b;
            --bubble-in: #182533;
            --bubble-out: #2b5278;
            --border: #111a24;
            --primary: #5288c1;
            --accent: #00f3ff;
            --text-main: #f5f5f5;
            --text-dim: #7f91a4;
            --radius: 12px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; }
        body { font-family: 'Outfit', sans-serif; background: var(--bg-dark); color: var(--text-main); height: 100dvh; overflow: hidden; display: flex; }
        #sidebar { width: 320px; background: var(--sidebar); border-right: 1px solid var(--border); z-index: 20; display: flex; flex-direction: column; }
        .brand-area { padding: 20px; font-family: 'JetBrains Mono'; font-weight: 800; color: var(--primary); border-bottom: 1px solid var(--border); }
        .nav-list { flex: 1; overflow-y: auto; }
        .nav-item { padding: 12px 16px; cursor: pointer; display: flex; align-items: center; gap: 12px; }
        .nav-item:hover { background: #202b36; }
        .nav-item.active { background: var(--primary); color: white; }
        #main-view { flex: 1; display: flex; flex-direction: column; position: relative; background: url('https://i.pinimg.com/736x/8c/98/99/8c98994518b575bfd81f75e96fe5a828.jpg'); background-size: cover; }
        header { height: 60px; background: var(--sidebar); display: flex; align-items: center; padding: 0 20px; border-bottom: 1px solid var(--border); justify-content: space-between; }
        #chat-feed { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 8px; }
        .msg-group { display: flex; gap: 10px; max-width: 80%; }
        .msg-group.me { align-self: flex-end; flex-direction: row-reverse; }
        .bubble { padding: 8px 12px; border-radius: 12px; position: relative; cursor: pointer; transition: filter 0.2s; }
        .msg-group.me .bubble { background: var(--bubble-out); border-bottom-right-radius: 4px; }
        .msg-group:not(.me) .bubble { background: var(--bubble-in); border-bottom-left-radius: 4px; }
        .msg-avatar { width: 35px; height: 35px; border-radius: 50%; object-fit: cover; align-self: flex-end; }
        .msg-meta { font-size: 0.65rem; color: var(--text-dim); text-align: right; margin-top: 4px; }
        .input-area { background: var(--sidebar); padding: 10px 15px; border-top: 1px solid var(--border); }
        .input-wrapper { display: flex; align-items: center; gap: 12px; }
        .chat-input-box { flex: 1; background: transparent; border: none; color: white; font-size: 1rem; }
        #context-menu { position: fixed; background: #1c242d; border: 1px solid #333; border-radius: 8px; z-index: 1000; display: none; flex-direction: column; width: 140px; box-shadow: 0 5px 15px rgba(0,0,0,0.5); }
        .menu-item { padding: 10px 15px; font-size: 0.9rem; cursor: pointer; }
        .menu-item:hover { background: #2b353f; }
        .menu-item.del { color: #ff5e5e; }
        #edit-bar { display: none; background: #17212b; padding: 5px 20px; border-top: 1px solid var(--primary); color: var(--primary); font-size: 0.8rem; justify-content: space-between; align-items: center; }
    </style>
</head>
<body>
    <div id="sidebar">
        <div class="brand-area">KUSTIFY HYPER-X</div>
        <div class="nav-list">
            <div id="group-list"></div>
            <div id="dm-list"></div>
        </div>
    </div>
    <div id="main-view">
        <header>
            <div id="header-title" style="font-weight:600"># Lobby</div>
            <button class="btn-icon" onclick="joinVoice()" style="background:none; border:none; color:var(--primary); font-size:1.2rem; cursor:pointer;">🎤</button>
        </header>
        <div id="chat-feed"></div>
        <div id="edit-bar">
            <span>Editing message...</span>
            <span onclick="cancelEdit()" style="cursor:pointer">✖</span>
        </div>
        <div class="input-area">
            <div class="input-wrapper">
                <button onclick="document.getElementById('file-input').click()" style="background:none; border:none; color:var(--text-dim); font-size:1.5rem; cursor:pointer;">📎</button>
                <input type="file" id="file-input" hidden onchange="handleFile()">
                <input type="text" id="msg-input" class="chat-input-box" placeholder="Write a message..." onkeydown="if(event.key==='Enter') sendMessage()">
                <button onclick="sendMessage()" style="background:none; border:none; color:var(--primary); font-weight:700; cursor:pointer;">SEND</button>
            </div>
        </div>
    </div>

    <div id="context-menu">
        <div class="menu-item" onclick="initEdit()">✏️ Edit</div>
        <div class="menu-item del" onclick="confirmDelete()">🗑️ Delete</div>
    </div>

    <script>
        const state = {
            user: localStorage.getItem("kv9_user") || "Anon",
            uid: localStorage.getItem("kv9_uid") || "u_" + Math.random().toString(36).substr(2, 8),
            pfp: localStorage.getItem("kv9_pfp") || "",
            group: "Lobby", dmTarget: null, ws: null, dms: {},
            activeMsgId: null
        };
        localStorage.setItem("kv9_uid", state.uid);

        window.onload = () => {
            loadGroups();
            connect(state.group);
        };

        function connect(group) {
            if(state.ws) state.ws.close();
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${group}/${state.uid}`);
            state.ws.onopen = () => state.ws.send(JSON.stringify({name: state.user, pfp: state.pfp}));
            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                if(d.type === "message") renderMessage(d);
                else if(d.type === "edit_message") updateMessageUI(d.id, d.text, true);
                else if(d.type === "delete_message") document.getElementById(`msg-group-${d.id}`)?.remove();
            };
        }

        function renderMessage(d) {
            const feed = document.getElementById('chat-feed');
            const isMe = (d.sender_id || d.user_id) === state.uid;
            const div = document.createElement('div');
            div.className = `msg-group ${isMe ? 'me' : ''}`;
            div.id = `msg-group-${d.id}`;
            div.innerHTML = `
                <div class="bubble" oncontextmenu="handleContext(event, '${d.id}', ${isMe})">
                    <div id="text-${d.id}">${marked.parse(d.text || '')}</div>
                    <div class="msg-meta">${new Date(d.timestamp*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})} ${d.edited ? '(edited)' : ''}</div>
                </div>
            `;
            feed.appendChild(div);
            feed.scrollTop = feed.scrollHeight;
        }

        function handleContext(e, id, isMe) {
            if(!isMe) return;
            e.preventDefault();
            state.activeMsgId = id;
            const menu = document.getElementById('context-menu');
            menu.style.display = 'flex';
            menu.style.left = e.pageX + 'px';
            menu.style.top = e.pageY + 'px';
            document.onclick = () => menu.style.display = 'none';
        }

        function initEdit() {
            const currentText = document.getElementById(`text-${state.activeMsgId}`).innerText;
            document.getElementById('msg-input').value = currentText;
            document.getElementById('edit-bar').style.display = 'flex';
            document.getElementById('msg-input').focus();
        }

        function cancelEdit() {
            state.activeMsgId = null;
            document.getElementById('edit-bar').style.display = 'none';
            document.getElementById('msg-input').value = '';
        }

        function sendMessage() {
            const inp = document.getElementById('msg-input');
            const txt = inp.value.trim();
            if(!txt) return;

            if(state.activeMsgId) {
                state.ws.send(JSON.stringify({type: "edit_message", message_id: state.activeMsgId, text: txt}));
                cancelEdit();
            } else {
                state.ws.send(JSON.stringify({type: "message", text: txt}));
            }
            inp.value = '';
        }

        function confirmDelete() {
            state.ws.send(JSON.stringify({type: "delete_message", message_id: state.activeMsgId}));
        }

        function updateMessageUI(id, text, edited) {
            const el = document.getElementById(`text-${id}`);
            if(el) {
                el.innerHTML = marked.parse(text);
                if(edited) {
                    const meta = el.nextElementSibling;
                    if(!meta.innerText.includes('(edited)')) meta.innerText += ' (edited)';
                }
            }
        }

        async function loadGroups() {
            const r = await fetch('/api/groups'); const d = await r.json();
            const l = document.getElementById('group-list'); l.innerHTML = '';
            d.groups.forEach(g => {
                const el = document.createElement('div'); el.className = `nav-item ${g===state.group?'active':''}`;
                el.innerHTML = `<span># ${g}</span>`;
                el.onclick = () => { state.group = g; connect(g); };
                l.appendChild(el);
            });
        }
        
        // ... [Rest of your utility functions remain exactly the same] ...
    </script>
</body>
</html>
"""
