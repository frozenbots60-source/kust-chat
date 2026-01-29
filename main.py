import os
import json
import asyncio
import boto3
import time
import re
from typing import List, Dict, Optional
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

# Initialize Redis & S3
redis = aioredis.from_url(REDIS_URL, decode_responses=True)
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
    config=Config(signature_version='s3v4')
)

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
        if user_id in self.global_lookup: del self.global_lookup[user_id]
        if user_id in self.user_meta: del self.user_meta[user_id]

    async def broadcast_presence(self, group_id: str):
        users = [m for uid, m in self.user_meta.items() if m.get("group") == group_id]
        payload = json.dumps({"type": "presence_update", "group_id": group_id, "count": len(users), "users": users})
        await self.broadcast_local(group_id, payload)

    async def broadcast_local(self, group_id: str, message: str):
        if group_id in self.active_connections:
            for connection in self.active_connections[group_id]:
                try: await connection.send_text(message)
                except: pass

    async def send_personal_message(self, target_id: str, message: str):
        if target_id in self.global_lookup:
            try: await self.global_lookup[target_id].send_text(message); return True
            except: return False
        return False

manager = ConnectionManager()

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            try:
                data = json.loads(message["data"])
                mtype = data.get("type")
                if mtype in ["message", "vc_signal_group", "edit_message", "delete_message"]:
                    await manager.broadcast_local(data.get("group_id"), message["data"])
                elif mtype == "dm":
                    await manager.send_personal_message(data.get("target_id"), message["data"])
            except: pass

@app.on_event("startup")
async def startup_event():
    if not await redis.sismember(GROUPS_KEY, "Lobby"): await redis.sadd(GROUPS_KEY, "Lobby")
    asyncio.create_task(redis_listener())

# API ENDPOINTS
@app.get("/api/groups")
async def get_groups():
    return {"groups": sorted(list(await redis.smembers(GROUPS_KEY)))}

@app.get("/api/history/{group_id}")
async def get_history(group_id: str, limit: int = 100):
    messages = await redis.lrange(f"{HISTORY_KEY}{group_id}", -limit, -1)
    return [json.loads(m) for m in messages]

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split('.')[-1]
    safe_name = f"{int(time.time())}_{os.urandom(4).hex()}.{ext}"
    file_key = f"kustify_v9/{safe_name}"
    s3_client.upload_fileobj(file.file, BUCKET_NAME, file_key, ExtraArgs={'ContentType': file.content_type})
    url = s3_client.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': file_key}, ExpiresIn=604800)
    return {"url": url}

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    try:
        init_data = await websocket.receive_json()
        name = init_data.get("name", "Anon").strip()
        user_info = {"id": user_id, "name": name, "pfp": init_data.get("pfp", "")}
        await manager.connect(websocket, group_id, user_info)
    except: return

    try:
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")

            if mtype == "message":
                data.update({"id": f"msg_{int(time.time()*1000)}", "user_id": user_id, "group_id": group_id, "timestamp": time.time()})
                await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(data))
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))

            elif mtype == "edit_message":
                msg_id = data.get("message_id")
                history_key = f"{HISTORY_KEY}{group_id}"
                msgs = await redis.lrange(history_key, 0, -1)
                for i, m_str in enumerate(msgs):
                    m = json.loads(m_str)
                    if m.get("id") == msg_id and m.get("user_id") == user_id:
                        m["text"] = data.get("new_text")
                        m["edited"] = True
                        await redis.lset(history_key, i, json.dumps(m))
                        await redis.publish(GLOBAL_CHANNEL, json.dumps({"type": "edit_message", "group_id": group_id, "id": msg_id, "text": m["text"]}))
                        break

            elif mtype == "delete_message":
                msg_id = data.get("message_id")
                history_key = f"{HISTORY_KEY}{group_id}"
                msgs = await redis.lrange(history_key, 0, -1)
                for m_str in msgs:
                    m = json.loads(m_str)
                    if m.get("id") == msg_id and m.get("user_id") == user_id:
                        await redis.lrem(history_key, 1, m_str)
                        await redis.publish(GLOBAL_CHANNEL, json.dumps({"type": "delete_message", "group_id": group_id, "id": msg_id}))
                        break
            
            elif mtype == "dm":
                data.update({"sender_id": user_id, "timestamp": time.time()})
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))
                await websocket.send_json(data)

            elif mtype in ["vc_join", "vc_leave", "vc_signal"]:
                data.update({"sender_id": user_id, "group_id": group_id})
                await redis.publish(GLOBAL_CHANNEL, json.dumps({**data, "type": "vc_signal_group"}))

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)
        await manager.broadcast_presence(group_id)

@app.get("/")
async def get(): return HTMLResponse(html_content)

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
        :root { --bg-dark: #050505; --panel: rgba(20, 20, 23, 0.85); --border: rgba(255, 255, 255, 0.08); --primary: #7000ff; --accent: #00f3ff; --text-main: #eeeeee; --text-dim: #888888; --glass: blur(20px) saturate(180%); --radius: 16px; }
        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; }
        body { font-family: 'Outfit', sans-serif; background: var(--bg-dark); color: var(--text-main); height: 100dvh; overflow: hidden; display: flex; }
        #sidebar { width: 280px; background: rgba(10, 10, 12, 0.8); backdrop-filter: var(--glass); border-right: 1px solid var(--border); z-index: 20; display: flex; flex-direction: column; transition: transform 0.3s ease; }
        .brand-area { padding: 24px; font-family: 'JetBrains Mono'; font-weight: 800; border-bottom: 1px solid var(--border); color: var(--primary); }
        .nav-list { flex: 1; padding: 15px; overflow-y: auto; }
        .nav-item { padding: 12px; border-radius: 8px; color: var(--text-dim); cursor: pointer; margin-bottom: 5px; }
        .nav-item.active { background: rgba(112, 0, 255, 0.1); color: var(--accent); border-left: 3px solid var(--accent); }
        #main-view { flex: 1; display: flex; flex-direction: column; position: relative; }
        header { height: 70px; display: flex; justify-content: space-between; align-items: center; padding: 0 20px; border-bottom: 1px solid var(--border); }
        #chat-feed { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 15px; }
        .msg-group { display: flex; gap: 12px; position: relative; }
        .msg-group.me { flex-direction: row-reverse; }
        .bubble { padding: 12px; border-radius: 12px; background: #1a1a1d; max-width: 70%; position: relative; }
        .me .bubble { background: var(--primary); }
        .msg-actions { display: none; position: absolute; top: -20px; right: 0; gap: 5px; }
        .msg-group:hover .msg-actions { display: flex; }
        .action-btn { background: #333; color: white; border: none; font-size: 0.7rem; padding: 2px 8px; border-radius: 4px; cursor: pointer; }
        .input-wrapper { padding: 20px; background: #000; border-top: 1px solid var(--border); display: flex; gap: 10px; }
        .chat-input-box { flex: 1; background: #111; border: 1px solid var(--border); color: #fff; padding: 12px 20px; border-radius: 30px; }
        .modal { position: fixed; inset: 0; background: #000; z-index: 100; display: flex; align-items: center; justify-content: center; }
        .btn-start { background: var(--primary); color: #fff; border: none; padding: 15px 40px; border-radius: 10px; font-weight: 700; cursor: pointer; }
        @media (max-width: 768px) { #sidebar { position: fixed; height: 100%; transform: translateX(-100%); } #sidebar.open { transform: translateX(0); } }
    </style>
</head>
<body>
    <div id="setup-modal" class="modal">
        <div style="text-align: center; width: 300px;">
            <h1 style="font-family: 'JetBrains Mono'; margin-bottom: 20px;">KUSTIFY_INIT</h1>
            <input type="text" id="username-input" style="width:100%; padding:15px; background:#111; border:1px solid #333; color:#fff; border-radius:10px; margin-bottom:15px;" placeholder="Enter Username">
            <button class="btn-start" onclick="saveUser()">CONNECT</button>
        </div>
    </div>

    <div id="sidebar">
        <div class="brand-area">KUSTIFY V9.0</div>
        <div class="nav-list" id="group-list"></div>
    </div>

    <div id="main-view">
        <header><h3 id="header-title"># Lobby</h3></header>
        <div id="chat-feed"></div>
        <div class="input-wrapper">
            <input type="text" id="msg-input" class="chat-input-box" placeholder="Write a message..." onkeypress="if(event.key==='Enter') sendMessage()">
            <button onclick="sendMessage()" style="background:var(--primary); border:none; color:#fff; width:45px; height:45px; border-radius:50%; cursor:pointer;">➤</button>
        </div>
    </div>

    <script>
        const state = {
            user: getCookie("k_user"),
            uid: getCookie("k_uid") || "u_" + Math.random().toString(36).substr(2, 9),
            group: "Lobby", ws: null
        };

        function setCookie(n, v) { const d = new Date(); d.setTime(d.getTime() + (30*24*60*60*1000)); document.cookie = `${n}=${v};expires=${d.toUTCString()};path=/`; }
        function getCookie(n) { const v = document.cookie.match('(^|;) ?' + n + '=([^;]*)(;|$)'); return v ? v[2] : null; }

        window.onload = () => {
            if(state.user) { 
                document.getElementById('setup-modal').style.display = 'none';
                init();
            }
        };

        function saveUser() {
            const n = document.getElementById('username-input').value.trim();
            if(!n) return;
            state.user = n;
            setCookie("k_user", n);
            setCookie("k_uid", state.uid);
            document.getElementById('setup-modal').style.display = 'none';
            init();
        }

        async function init() {
            const r = await fetch('/api/groups'); const d = await r.json();
            const gl = document.getElementById('group-list');
            d.groups.forEach(g => {
                const el = document.createElement('div'); el.className = `nav-item ${g===state.group?'active':''}`;
                el.innerText = `# ${g}`; el.onclick = () => { state.group = g; connect(); };
                gl.appendChild(el);
            });
            connect();
        }

        function connect() {
            if(state.ws) state.ws.close();
            document.getElementById('chat-feed').innerHTML = '';
            fetch(`/api/history/${state.group}`).then(r=>r.json()).then(m => m.forEach(renderMessage));
            
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${state.group}/${state.uid}`);
            state.ws.onopen = () => state.ws.send(JSON.stringify({name: state.user, pfp: ""}));
            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                if(d.type === "message") renderMessage(d);
                if(d.type === "edit_message") {
                    const el = document.querySelector(`[data-id="${d.id}"] .bubble`);
                    if(el) el.innerHTML = marked.parse(d.text) + ' <small style="opacity:0.5">(edited)</small>';
                }
                if(d.type === "delete_message") document.querySelector(`[data-id="${d.id}"]`)?.remove();
            };
        }

        function renderMessage(d) {
            const feed = document.getElementById('chat-feed');
            const isMe = d.user_id === state.uid;
            const div = document.createElement('div');
            div.className = `msg-group ${isMe ? 'me' : ''}`;
            div.setAttribute('data-id', d.id);
            div.innerHTML = `
                <div class="bubble">${marked.parse(d.text)}${d.edited ? ' <small style="opacity:0.5">(edited)</small>' : ''}</div>
                ${isMe ? `<div class="msg-actions">
                    <button class="action-btn" onclick="editMsg('${d.id}')">Edit</button>
                    <button class="action-btn" style="color:#ff4444" onclick="deleteMsg('${d.id}')">Del</button>
                </div>` : ''}
            `;
            feed.appendChild(div); feed.scrollTop = feed.scrollHeight;
        }

        function sendMessage() {
            const inp = document.getElementById('msg-input');
            if(!inp.value.trim()) return;
            state.ws.send(JSON.stringify({type: "message", text: inp.value, user_name: state.user}));
            inp.value = '';
        }

        function editMsg(id) {
            const old = document.querySelector(`[data-id="${id}"] .bubble`).innerText.replace('(edited)', '').trim();
            const n = prompt("Edit message:", old);
            if(n && n !== old) state.ws.send(JSON.stringify({type: "edit_message", message_id: id, new_text: n}));
        }

        function deleteMsg(id) {
            if(confirm("Delete message?")) state.ws.send(JSON.stringify({type: "delete_message", message_id: id}));
        }
    </script>
</body>
</html>
"""
