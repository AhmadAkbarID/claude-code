import asyncio
import json
import logging
import re
import os
import time
import datetime
import subprocess
import aiohttp
from playwright.async_api import async_playwright, Error as PlaywrightError
import redis.asyncio as redis_async
from redis.exceptions import ConnectionError, TimeoutError

# --- KONFIGURASI ZONA WAKTU (UTC+7 / WIB) ---
TZ_WIB = datetime.timezone(datetime.timedelta(hours=7))
logging.Formatter.converter = lambda *args: datetime.datetime.now(TZ_WIB).timetuple()

# --- KONFIGURASI LOGGING, REDIS & TELEGRAM ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [GV-TRUNKING] - %(message)s')
# Pastikan URL Redis sesuai dengan IP VPS Telpony.com Anda
REDIS_URL = "redis://:GunaPediaRedis2026!@178.156.166.222:6379/0"

TG_TOKEN = "8718223346:AAESIPsCn6mv5xgePDQDVhb78hIRVG_4wk4" # Trunking 1

TG_ADMIN_IDS = ["8410497466", "7869490663"]

import urllib.request
import socket
import re
import json
import ssl  # 🔥 Tambahan modul SSL

# --- SETUP NODE ID UNTUK CLUSTER MULTI-RDP ---
def get_ip():
    # 🔥 ANTI-SSL ERROR: Abaikan validasi sertifikat yang sering gagal di Windows RDP
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Daftar URL: Menggabungkan HTTP murni dan paksaan IPv4
    endpoints = [
        'http://api.ipify.org',       # HTTP biasa (Paling kebal error SSL)
        'https://ipv4.icanhazip.com', # Paksa server merespons dengan IPv4
        'https://v4.ident.me',        # Paksa IPv4
        'http://ifconfig.me/ip'
    ]
    
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            # Waktu tunggu diperpanjang menjadi 10 detik + masukkan Bypass SSL
            with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
                ip = response.read().decode('utf-8').strip()
                if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                    return ip
        except Exception as e:
            # Uncomment baris bawah jika ingin melihat alasan error di console:
            # print(f"Gagal hit {url}: {e}")
            continue
            
    # Fallback terakhir menggunakan ipinfo
    try:
        req = urllib.request.Request('http://ipinfo.io/json', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            data = json.loads(response.read().decode('utf-8'))
            ip = data.get('ip', '').strip()
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                return ip
    except Exception:
        pass

    return 'Local'

MY_IP = get_ip()
ip_parts = MY_IP.split('.')
NODE_ID = ip_parts[-1] if len(ip_parts) == 4 else str(MY_IP)

# --- GLOBAL STATE (MULTITENANT LACI MEMORI) ---
# Diubah menjadi Dictionary agar memori antar-Trunking tidak saling tertukar saat jalan bareng!
GLOBAL_STATE = {}
ACTIVE_TASKS = set() # 🔥 FIX: Pelindung Task agar tidak dibunuh Garbage Collector

def init_state(session_id):
    if session_id not in GLOBAL_STATE:
        GLOBAL_STATE[session_id] = {
            "CACHED_PAYLOAD": None,
            "BACKOFF_TIME": 5,
            "LAST_ACTIVITY_TS": time.time(),
            "LAST_CALL_TS": "Belum ada trafik",
            "BINDING_EXPOSED": False,
            "REDIS_TASK": None,
            "STATUS": "INIT ⚙️",
            "IS_DEDICATED": False,  # 🔥 V7: Flag laci armada Dedicated
            "KILLED": False         # 🔥 V7: Flag penutup armada paksa
        }

# --- TELEGRAM LIGHTWEIGHT ENGINE ---
async def send_tg_msg(chat_id, text, reply_markup=None):
    """Pengirim pesan Telegram Asinkron (Anti-Lag)"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    try:
        # 🔥 FIX SSL: Mengabaikan sertifikat self-signed RDP Windows 🔥
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            await session.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.warning(f"⚠️ Gagal mengirim pesan Telegram: {e}")

async def send_tg_msg_direct(session, chat_id, text, reply_markup=None):
    """Pengirim pesan internal untuk Leader (tanpa membuat session baru)"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    try: await session.post(url, json=payload, timeout=5)
    except Exception: pass

# ====================================================================
# TELEGRAM LEADER ELECTION & DISTRIBUTOR COMMANDS
# ====================================================================
async def telegram_leader_poller():
    """Hanya 1 Node RDP (Leader) yang menarik pesan dari Telegram, lalu menyebarkannya ke Redis"""
    redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)
    offset = 0
    try:
        raw_offset = await redis_client.get("harvester_tg_offset")
        if raw_offset: offset = int(raw_offset)
    except: pass

    logging.info(f"🤖 Telegram Commander Standby. Mencoba menjadi Leader...")
    
    while True:
        try:
            # Rebut atau perpanjang takhta Leader
            is_leader = await redis_client.set("harvester_tg_leader", MY_IP, nx=True, ex=30)
            if not is_leader:
                current_leader = await redis_client.get("harvester_tg_leader")
                if current_leader == MY_IP:
                    await redis_client.expire("harvester_tg_leader", 30)
                    is_leader = True

            if is_leader:
                url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={offset}&timeout=15"
                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(url) as resp:
                        data = await resp.json()
                        if data.get("ok"):
                            for result in data.get("result", []):
                                offset = result["update_id"] + 1
                                await redis_client.set("harvester_tg_offset", offset)
                                
                                msg = result.get("message")
                                cb = result.get("callback_query")
                                
                                chat_id, text = None, ""
                                if msg and "text" in msg:
                                    chat_id = str(msg["chat"]["id"])
                                    text = msg["text"].strip()
                                elif cb:
                                    chat_id = str(cb["message"]["chat"]["id"])
                                    text = cb["data"].strip()
                                    await session.post(f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=5)
                                
                                if chat_id and text:
                                    is_admin = (chat_id in TG_ADMIN_IDS)
                                    parts = text.split(" ")
                                    cmd = parts[0]
                                    
                                    # 1. Menu Navigasi (Hanya Leader yang membalas, tidak di-broadcast)
                                    if cmd == "/start":
                                        if is_admin:
                                            menu = {"inline_keyboard": [[{"text": "📊 Status Reguler", "callback_data": "/status"}, {"text": "🌟 Status Dedicated", "callback_data": "/dedicated"}], [{"text": "➕ Tambah Trunk", "callback_data": "cmd_help_add"}, {"text": "⚙️ Kelola Trunk", "callback_data": "cmd_help_manage"}]]}
                                            await send_tg_msg_direct(session, chat_id, "👋 <b>Selamat datang, Bosku!</b>\nSilakan kendalikan armada Telpony.com Anda melalui menu di bawah ini:", reply_markup=menu)
                                        else:
                                            await send_tg_msg_direct(session, chat_id, "👋 <b>Halo!</b>\nAnda memiliki akses pantau terbatas.\nKetik /status untuk melihat kondisi Trunking saat ini.")
                                    
                                    elif cmd == "cmd_help_add" and is_admin:
                                        await send_tg_msg_direct(session, chat_id, "💡 <b>Cara Menambah Trunk:</b>\n\nFormat baru untuk Multi-Node:\n<code>/add [NodeID] [Trunk]</code>\n\nContoh menyalakan va1 di RDP berakhiran IP 43:\n<code>/add 43 va1</code>\n<code>/add 43 va1-va5</code>")
                                    elif cmd == "cmd_help_manage" and is_admin:
                                        await send_tg_msg_direct(session, chat_id, "⚙️ <b>Manajemen Armada:</b>\n\n<b>Pindah ke Dedicated:</b>\n<code>/dedicate 43 va1</code>\n\n<b>Hapus dari Dedicated:</b>\n<code>/undedicate 43 va1</code>\n\n<b>Tutup/Matikan Trunk:</b>\n<code>/close 43 va1</code>")
                                    
                                    # 2. Perintah Broadcast Keseluruhan Node (Semua RDP Menjawab)
                                    elif cmd in ["/status", "/dedicated"]:
                                        payload = {"cmd": cmd, "chat_id": chat_id, "is_admin": is_admin, "args": ""}
                                        await redis_client.publish("harvester_cmd_all", json.dumps(payload))
                                    
                                    # 3. Perintah Eksekusi Spesifik per Node RDP
                                    elif cmd in ["/add", "/close", "/dedicate", "/undedicate"] and is_admin:
                                        if len(parts) >= 3:
                                            target_node = parts[1]
                                            args = parts[2]
                                            payload = {"cmd": cmd, "chat_id": chat_id, "is_admin": is_admin, "args": args}
                                            await redis_client.publish(f"harvester_cmd_{target_node}", json.dumps(payload))
                                        else:
                                            await send_tg_msg_direct(session, chat_id, f"ℹ️ Format salah. Anda menggunakan arsitektur Multi-Node.\n\nContoh Benar:\n<code>{cmd} {NODE_ID} va1</code>")
            await asyncio.sleep(0.5)
        except Exception as e:
            await asyncio.sleep(2)

async def redis_command_listener():
    """Seluruh Node mendengarkan perintah spesifik mereka dari saluran Redis"""
    redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("harvester_cmd_all", f"harvester_cmd_{NODE_ID}")
    
    logging.info(f"👂 Pendengar Komando aktif pada Node-{NODE_ID}")
    
    while True:
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message["type"] == "message":
                data = json.loads(message["data"])
                await handle_tg_command_local(data["chat_id"], data["cmd"], data["args"], data["is_admin"])
            await asyncio.sleep(0.1)
        except Exception:
            await asyncio.sleep(2)

async def handle_tg_command_local(chat_id, cmd, args, is_admin):
    """Pengeksekusi perintah lokal di masing-masing mesin RDP"""
    
    if cmd == "/status":
        reply = f"📊 <b>STATUS ARMADA (NODE-{NODE_ID})</b>\n\n"
        count = 0
        if GLOBAL_STATE:
            for sid, state in GLOBAL_STATE.items():
                if state.get("IS_DEDICATED") or state.get("KILLED"): continue
                reply += f"🔹 <b>{sid}</b>: {state.get('STATUS', 'UNKNOWN')}\n   └ <i>Call: {state.get('LAST_CALL_TS', 'Belum ada trafik')}</i>\n"
                count += 1
        
        # Jika loop di atas tidak menemukan armada aktif, laporkan sebagai kosong
        if count == 0:
            reply += "📭 <i>Belum ada armada reguler yang aktif di node ini.</i>"
            
        await send_tg_msg(chat_id, reply)

    elif cmd == "/dedicated":
        reply = f"🌟 <b>STATUS DEDICATED (NODE-{NODE_ID})</b>\n\n"
        count = 0
        if GLOBAL_STATE:
            for sid, state in GLOBAL_STATE.items():
                if not state.get("IS_DEDICATED") or state.get("KILLED"): continue
                reply += f"⭐ <b>{sid}</b>: {state.get('STATUS', 'UNKNOWN')}\n   └ <i>Call: {state.get('LAST_CALL_TS', 'Belum ada trafik')}</i>\n"
                count += 1
                
        # Jika loop di atas tidak menemukan armada dedicated, laporkan sebagai kosong
        if count == 0:
            reply += "📭 <i>Belum ada armada dedicated yang aktif di node ini.</i>"
            
        await send_tg_msg(chat_id, reply)

    elif cmd == "/add" and is_admin:
        new_sids = parse_sessions(args)
        added = []
        for sid in new_sids:
            if sid not in GLOBAL_STATE or GLOBAL_STATE[sid].get("KILLED"):
                if sid in GLOBAL_STATE:
                    GLOBAL_STATE[sid]["KILLED"] = False
                    GLOBAL_STATE[sid]["STATUS"] = "STARTING 🟡"
                
                task = asyncio.create_task(keep_alive_harvester(sid))
                ACTIVE_TASKS.add(task)
                task.add_done_callback(ACTIVE_TASKS.discard)
                added.append(sid)
                await asyncio.sleep(3) 
        
        if added:
            await send_tg_msg(chat_id, f"✅ <b>[NODE-{NODE_ID}]</b> Berhasil menyalakan:\n<b>{', '.join(added)}</b>")
        else:
            await send_tg_msg(chat_id, f"⚠️ <b>[NODE-{NODE_ID}]</b> Semua Trunk tersebut sudah aktif.")

    elif cmd == "/close" and is_admin:
        targets = parse_sessions(args)
        closed = []
        for sid in targets:
            if sid in GLOBAL_STATE and not GLOBAL_STATE[sid].get("KILLED"):
                GLOBAL_STATE[sid]["KILLED"] = True
                GLOBAL_STATE[sid]["STATUS"] = "OFFLINE 🔴"
                try:
                    cmd_kill = f'wmic process where "name=\'chrome.exe\' and commandline like \'%gv_profile_{sid}%\'" call terminate'
                    subprocess.run(cmd_kill, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except: pass
                closed.append(sid)
        
        if closed: await send_tg_msg(chat_id, f"🛑 <b>[NODE-{NODE_ID}]</b> Berhasil menutup paksa:\n<b>{', '.join(closed)}</b>")

    elif cmd == "/dedicate" and is_admin:
        targets = parse_sessions(args)
        moved = []
        for sid in targets:
            if sid in GLOBAL_STATE:
                GLOBAL_STATE[sid]["IS_DEDICATED"] = True
                moved.append(sid)
        if moved: await send_tg_msg(chat_id, f"🌟 <b>[NODE-{NODE_ID}]</b> Dipindahkan ke Dedicated:\n<b>{', '.join(moved)}</b>")

    elif cmd == "/undedicate" and is_admin:
        targets = parse_sessions(args)
        moved = []
        for sid in targets:
            if sid in GLOBAL_STATE:
                GLOBAL_STATE[sid]["IS_DEDICATED"] = False
                moved.append(sid)
        if moved: await send_tg_msg(chat_id, f"↩️ <b>[NODE-{NODE_ID}]</b> Dikembalikan ke Reguler:\n<b>{', '.join(moved)}</b>")
# --- FITUR SMS: DOM AUTOMATION DENGAN NETWORK VALIDATION ---
async def send_sms_via_ui(browser_context, session_id, to_number, message_text):
    """Mengirim SMS dengan sistem Auto-Retry, Koma (,), dan validasi Network Intercept"""
    logging.info(f"✉️ [SMS ENGINE {session_id}] Memulai pengiriman SMS ke {to_number}...")
    sms_page = await browser_context.new_page()
    max_retries = 3

    try:
        for attempt in range(1, max_retries + 1):
            try:
                logging.info(f"🔄 [SMS ATTEMPT {attempt}/{max_retries} {session_id}] Membuka panel pesan...")
                await sms_page.goto("https://voice.google.com/u/0/messages", wait_until="domcontentloaded")
                await asyncio.sleep(2) # Beri waktu render awal
                
                # 1. KLIK TOMBOL NEW MESSAGE (Menggunakan A11y Label murni dari dump HTML)
                new_msg_btn = sms_page.locator('div[aria-label="Send new message"]').first
                await new_msg_btn.wait_for(state="visible", timeout=15000)
                await new_msg_btn.click()
                await asyncio.sleep(1.5) 
                
                # 2. KETIK NOMOR TUJUAN (Menggunakan Placeholder murni dari dump HTML)
                to_input = sms_page.locator('input[placeholder="Type a name or phone number"]').first
                await to_input.wait_for(state="visible", timeout=10000)
                await to_input.fill(to_number)
                await asyncio.sleep(0.5)
                
                # 🔥 RAHASIA UI GOOGLE VOICE: Tekan koma agar input di-lock menjadi kontak "Chip"
                await sms_page.keyboard.press(",")
                await asyncio.sleep(1)
                
                await sms_page.keyboard.press("Enter")
                await asyncio.sleep(1.5)
                
                # 3. KETIK PESAN (Menggunakan Placeholder murni dari dump HTML)
                msg_input = sms_page.locator('textarea[placeholder="Type a message"]').first
                await msg_input.wait_for(state="visible", timeout=10000)
                await msg_input.fill(message_text)
                await asyncio.sleep(0.5)
                
                # 4. TEKAN ENTER & VALIDASI JARINGAN (Menunggu Request POST ke /sendsms)
                logging.info(f"⏳ [SMS VALIDATION {session_id}] Menunggu konfirmasi request jaringan dari Google...")
                
                # Menggunakan expect_request untuk menangkap request yang keluar
                async with sms_page.expect_request(lambda request: "sendsms" in request.url and request.method == "POST", timeout=10000) as req_info:
                    await sms_page.keyboard.press("Enter")
                
                # Jika tidak timeout, berarti request berhasil ditangkap!
                req = await req_info.value
                if req:
                    logging.info(f"✅ [SMS SUCCESS {session_id}] Request API 'sendsms' tervalidasi! Pesan resmi terkirim ke {to_number}.")
                    await asyncio.sleep(2) # Beri jeda sebelum tutup tab agar request tuntas
                    break # Keluar dari loop retry karena sukses
                
            except Exception as e:
                logging.warning(f"⚠️ [SMS FAILED {session_id}] Percobaan {attempt} gagal: {e}")
                if attempt < max_retries:
                    logging.info(f"🔁 [SMS RETRY {session_id}] Mencoba ulang dalam 3 detik...")
                    await asyncio.sleep(3)
                else:
                    logging.error(f"❌ [SMS FATAL {session_id}] Gagal mengirim SMS ke {to_number} setelah {max_retries} percobaan penuh.")

    finally:
        try:
            await sms_page.close()
        except Exception:
            pass

# --- CORE TRUNKING ENGINE ---
async def process_gv_response(response, redis_client, session_id, page):
    """Mencegat inisialisasi akun Google Voice untuk sinkronisasi awal domain ke VPS"""
    state = GLOBAL_STATE[session_id]
    
    # 🔥 ANTI-SPAM PJSIP RELOAD 🔥
    # Jika KTP sudah tersimpan, hiraukan ping rutin Google agar Asterisk tidak mabok di-reload!
    if state["CACHED_PAYLOAD"] is not None:
        return False

    url = response.url
    if "voice/v1/voiceclient/account/get" in url or "voiceclient/threadinginfo/get" in url:
        try:
            raw_text = await response.text()
            # Bersihkan prefix anti-JSON-Hijacking bawaan Google
            clean_text = re.sub(r"^\)]\}'\s*", "", raw_text)
            
            # Ekstraksi domain PBX Google
            domain_match = re.search(r'"([^"]+\.w\.pbx\.voice\.sip\.google\.com)"', clean_text)
            
            if domain_match:
                domain = domain_match.group(1)
                user_agent = await page.evaluate("navigator.userAgent")
                headers = response.request.headers
                client_info = headers.get("x-google-client-info", "")
                
                payload = {
                    "gv_id": session_id,  
                    "domain": domain,
                    "user_agent": user_agent,
                    "x_google_client_info": client_info
                }

                state["CACHED_PAYLOAD"] = payload # 🔥 Simpan KTP ke laci memori! 🔥
                state["BACKOFF_TIME"] = 5         # 🔥 Reset Timer Backoff karena WSS sehat! 🔥
                
                if state["STATUS"] in ["STARTING 🟡", "DEFIBRILLATING ⚡", "INIT ⚙️"]:
                    state["STATUS"] = "RUNNING 🟢"

                # Sinkronisasi status ke Redis agar VPS siap siaga
                await redis_client.setex(f"gvoice_status:{session_id}", 3600, domain)
                await redis_client.lpush("gv_token_updates", json.dumps(payload))
                logging.info(f"🎯 [BINGO] Payload inisialisasi [{session_id}] berhasil didorong ke VPS!")
                return True 
        except Exception:
            pass
    return False

async def run_harvester(session_id):
    """Fungsi Core Harvester yang dibungkus untuk keperluan Hard Restart"""
    init_state(session_id)
    state = GLOBAL_STATE[session_id]
    
    state["LAST_ACTIVITY_TS"] = time.time() # Reset watchdog di awal sesi
    state["BINDING_EXPOSED"] = False # Wajib di-reset agar Browser Context yang baru tetap disuntik radar!
    state["STATUS"] = "STARTING 🟡"

    profile_dir = os.path.join(os.getcwd(), f"gv_profile_{session_id}")
    if not os.path.exists(profile_dir):
        logging.info(f"📁 Membuat profil browser baru untuk: {session_id}")
    else:
        logging.info(f"♻️ Memuat profil browser yang sudah ada untuk: {session_id}")

    # 2. KONEK KE REDIS GUNAPEDIA DENGAN PARAMETER ANTI-BADAI
    # FIX: Hapus health_check_interval yang bermasalah dan pasang ConnectionPool
    # agar tidak kena "MaxConnectionsError" saat Chrome menembak banyak paket SIP
    pool = redis_async.ConnectionPool.from_url(
        REDIS_URL, 
        decode_responses=True,
        socket_keepalive=True,
        retry_on_timeout=True,
        socket_connect_timeout=5,
        max_connections=5000  # 1000 koneksi sudah sangat lega untuk level RDP
    )
    redis_client = redis_async.Redis(connection_pool=pool)
    await redis_client.ping()
    logging.info(f"✅ [{session_id}] Terhubung ke Redis Telpony.com VPS (High-Concurrency Pool)")

    # 🔥 SUNTIKAN VACCINE: Blok Try-Finally untuk mencegah Socket Exhaustion (WinError 10055) 🔥
    try:
        async with async_playwright() as p:
            # 🔥 PILAR 1: SUNTIKAN ANTI-TIDUR (Mencegah Chrome Narcolepsy) 🔥
            browser_context = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False, 
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-timer-throttling",      # Paksa JS tetap jalan di background
                    "--disable-backgrounding-occluded-windows",   # Paksa render walau ketutup tab lain
                    "--disable-renderer-backgrounding"            # Paksa performa CPU 100%
                ]
            )
            
            # 🔥 PILLAR V2: CONSOLE SPY 🔥
            async def handle_console(msg):
                if "telephony" in msg.text or "WSS" in msg.text:
                    logging.info(f"🖥️ [BROWSER CONSOLE {session_id}] {msg.type.upper()}: {msg.text}")
            
            browser_context.on("console", handle_console)

            # 🔥 HOOK JAVASCRIPT STEALTH: FIX SCOPE REFERENCE ERROR 🔥
            js_stealth_hook = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            
            window.native_ws = null;
            const NativeWebSocket = window.WebSocket;
            
            // 🔥 FIX SCOPE MUTLAK: Simpan referensi fungsi asli ke objek GLOBAL window 🔥
            window.origSend = NativeWebSocket.prototype.send;
            
            // 1. Sadap Level Konstruktor 
            window.WebSocket = new Proxy(NativeWebSocket, {
                construct(target, args) {
                    const ws = new target(...args);
                    const url = args[0];
                    
                    if (url && url.includes('telephony.goog')) {
                        window.native_ws = ws;
                        
                        ws.addEventListener('close', (e) => {
                            console.warn("WSS Terputus!", e);
                            if (window.python_rx) window.python_rx("SYS", "WSS_DEAD");
                        });

                        ws.addEventListener('message', async (event) => {
                            try {
                                let text = typeof event.data === 'string' ? event.data : await new Response(event.data).text();
                                if (window.python_rx) window.python_rx("RX", text);
                            } catch(e) { console.error("RX Intercept Error", e); }
                        });
                    }
                    return ws;
                }
            });
            
            // 2. Sadap Level Prototype 
            NativeWebSocket.prototype.send = function(data) {
                if (this.url && this.url.includes('telephony.goog')) {
                    window.native_ws = this; 
                    try {
                        if (typeof data === 'string') {
                            if (window.python_rx) window.python_rx("TX", data);
                        } else if (data instanceof Blob) {
                            data.text().then(t => { if(window.python_rx) window.python_rx("TX", t); });
                        } else {
                            let text = new TextDecoder().decode(data);
                            if (window.python_rx) window.python_rx("TX", text);
                        }
                    } catch(e) { console.error("TX Intercept Error", e); }
                }
                return window.origSend.apply(this, arguments); // Pakai referensi global
            };
            
            // 3. Injector WSS dari Python ke Chrome
            window.send_to_google = (msg) => {
                if(window.native_ws && window.native_ws.readyState === 1) {
                    window.origSend.call(window.native_ws, msg); // Panggil via referensi global!
                } else {
                    console.error("Native WSS belum siap atau terputus!");
                    // 🔥 FIX: Paksa lapor ke Python untuk DEFIBRILLATOR! 🔥
                    if (window.python_rx) window.python_rx("SYS", "WSS_DEAD"); 
                }
            };
            """
            await browser_context.add_init_script(js_stealth_hook)
            
            # 🔥 FIX 1: TANGKAP TAB GAIB (GHOST TABS) 🔥
            pages = browser_context.pages
            if len(pages) > 0:
                page = pages[0] # Pakai tab yang di-restore Chrome
            else:
                page = await browser_context.new_page() # Buat baru kalau kosong
                
            await page.bring_to_front() # Cegah Chrome membekukan tab

            # 🔥 FUNGSI AUTO-DEFIBRILLATOR 🔥
            async def trigger_defibrillator(reason="Unknown"):
                if state.get("KILLED"): return
                try:
                    state["STATUS"] = "DEFIBRILLATING ⚡"
                    # Silent Healing: Tidak mengirim ke Telegram agar tidak spam
                    logging.info(f"⚡ [DEFIBRILLATOR {session_id}] Alasan: {reason}. Menunggu {state['BACKOFF_TIME']} detik (Anti-Spam)...")
                    await asyncio.sleep(state["BACKOFF_TIME"])
                    state["CACHED_PAYLOAD"] = None # Amputasi KTP lama agar Chrome minta rute baru
                    await page.reload(wait_until="domcontentloaded")
                    logging.info(f"⚡ [DEFIBRILLATOR {session_id}] SUKSES! Menunggu WSS baru tercipta...")
                    state["STATUS"] = "RUNNING 🟢"
                except PlaywrightError as e:
                    logging.error(f"❌ [DEFIBRILLATOR {session_id}] Gagal! Tab mungkin tertutup manual: {e}")
                except Exception as e:
                    logging.error(f"❌ [DEFIBRILLATOR {session_id}] Error Sistem: {e}")
                    state["BACKOFF_TIME"] = min(state["BACKOFF_TIME"] * 2, 300) # Max backoff 5 menit

            # 🔥 FIX 2: BINDING KE LEVEL BROWSER 🔥
            if not state["BINDING_EXPOSED"]:
                async def handle_python_rx(source, direction, data):
                    if state.get("KILLED"): return
                    async def _process_payload():
                        # Watchdog: Reset timer setiap ada pertukaran data
                        if direction in ["RX", "TX"]:
                            state["LAST_ACTIVITY_TS"] = time.time()

                        # Tangkap teriakan dari radar Javascript
                        if direction == "SYS" and data == "WSS_DEAD":
                            logging.warning(f"🚨 [RADAR {session_id}] Koneksi WSS Asli Chrome Mati! Memicu Defibrillator...")
                            asyncio.create_task(trigger_defibrillator("WSS Chrome Terputus Fisik/Belum Siap"))
                            return

                        if not data or len(str(data).strip()) < 5: return
                        first_line = str(data).splitlines()[0]
                        icon = "↙️ [RX]" if direction == "RX" else "↗️ [TX]"
                        
                        # 🔥 FIX: RADAR TRAFIK CALL DIPERTajam DENGAN JAM WIB 🔥
                        if first_line.startswith("INVITE"):
                            state["STATUS"] = "IN USE 🔵"
                            state["LAST_CALL_TS"] = datetime.datetime.now(TZ_WIB).strftime("%H:%M:%S")
                        elif first_line.startswith("BYE") or first_line.startswith("CANCEL") or "SIP/2.0 487" in first_line or "SIP/2.0 486" in first_line or "SIP/2.0 603" in first_line or "SIP/2.0 404" in first_line:
                            state["STATUS"] = "RUNNING 🟢"
                            state["LAST_CALL_TS"] = datetime.datetime.now(TZ_WIB).strftime("%H:%M:%S")

                        if direction == "RX" or (direction == "TX" and first_line.startswith("REGISTER")):
                            logging.info(f"{icon} [{session_id}] {first_line}")
                            try:
                                # 🛡️ BLOK TRY-EXCEPT AGAR TASK TIDAK MATI KENA WINERROR 121
                                await redis_client.publish(f"gv_rx_{session_id}", data)
                            except (ConnectionError, TimeoutError, OSError) as e:
                                logging.warning(f"⚠️ [{session_id}] Gagal mengirim RX/TX ke VPS (Jaringan RDP nge-lag/putus): {e}")
                        else:
                            pass
                            
                    asyncio.create_task(_process_payload())
                    
                await browser_context.expose_binding("python_rx", handle_python_rx)
                state["BINDING_EXPOSED"] = True

            # 🔥 TASK PENDENGAR REDIS: ANTI-BADAI & AUTO-ABSEN (RESEND KTP) 🔥
            async def redis_to_browser():
                while not state.get("KILLED"):
                    try:
                        # Jika script baru reconnect (putus-nyambung), setor KTP ulang ke VPS!
                        if state["CACHED_PAYLOAD"]:
                            logging.info(f"🔄 [AUTO-ABSEN] Koneksi pulih! Menyetor ulang KTP [{session_id}] ke VPS...")
                            await redis_client.setex(f"gvoice_status:{session_id}", 3600, state["CACHED_PAYLOAD"]["domain"])
                            await redis_client.lpush("gv_token_updates", json.dumps(state["CACHED_PAYLOAD"]))

                        pubsub = redis_client.pubsub()
                        # 🔥 FIX SMS: Menambahkan channel SMS ke dalam daftar langganan
                        await pubsub.subscribe(f"gv_tx_{session_id}", f"gv_sms_tx_{session_id}", "gv_system_broadcast")
                        logging.info(f"📡 [REDIS {session_id}] Siap menerima perintah (SIP INJECT & SMS) dari VPS...")
                        
                        # 🔥 PILLAR V2: NON-BLOCKING PUBSUB (Anti Gantung) 🔥
                        while not state.get("KILLED"):
                            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                            if message:
                                channel = message['channel']
                                msg_data = message['data']

                                # Sinyal Amnesia / KTP Demand
                                if channel == "gv_system_broadcast" and msg_data in ["VPS_REBOOTED", f"KTP_DEMAND_{session_id}"]:
                                    logging.warning(f"🚨 [VPS SOS {session_id}] Sinyal diterima: {msg_data}")
                                    logging.warning(f"⚡ [{session_id}] Harus minta Rute Kunci baru dari Google. Memicu Defibrillator...")
                                    asyncio.create_task(trigger_defibrillator(f"Sinyal SOS dari VPS ({msg_data})"))
                                
                                # 🔥 FITUR SMS: Menangkap JSON instruksi SMS dari VPS 🔥
                                elif channel == f"gv_sms_tx_{session_id}":
                                    try:
                                        sms_payload = json.loads(msg_data)
                                        to_number = sms_payload.get("to")
                                        message_text = sms_payload.get("message")
                                        if to_number and message_text:
                                            # Lempar tugas SMS ke background agar tidak memblokir antrean SIP
                                            task = asyncio.create_task(send_sms_via_ui(browser_context, session_id, to_number, message_text))
                                            ACTIVE_TASKS.add(task)
                                            task.add_done_callback(ACTIVE_TASKS.discard)
                                    except Exception as e:
                                        logging.error(f"❌ [SMS PAYLOAD ERROR {session_id}] JSON tidak valid: {e}")

                                # Sinyal Inject SIP
                                elif channel == f"gv_tx_{session_id}":
                                    logging.info(f"💉 [INJECT {session_id}] Meneruskan paket Asterisk ke WSS Chrome asli...")
                                    try:
                                        await page.evaluate("(msg) => window.send_to_google(msg)", msg_data)
                                    except PlaywrightError as e:
                                        # 🔥 PROTEKSI ERROR CERDAS: Pisahkan JS Error dan Tab Error 🔥
                                        if "ReferenceError" in str(e) or "SyntaxError" in str(e) or "TypeError" in str(e):
                                            logging.error(f"❌ [JS ERROR {session_id}] Kegagalan eksekusi skrip di dalam browser: {e}")
                                        else:
                                            logging.error(f"❌ [HUMAN ERROR {session_id}] Tab sepertinya ditutup manual! Error: {e}")
                                            break # Keluar dari loop PubSub untuk memicu Hard Restart
                                
                                continue # 🔥 FIX ZERO-LATENCY: Langsung tarik pesan SIP berikutnya tanpa delay!

                            await asyncio.sleep(0.1) # Napas CPU hanya dieksekusi jika antrean sedang KOSONG

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logging.warning(f"⚠️ [{session_id}] Koneksi Listener Redis terputus ({e}). Mencoba auto-reconnect dalam 5 detik...")
                        await asyncio.sleep(5)

            state["REDIS_TASK"] = asyncio.create_task(redis_to_browser())

            # Pasang Interceptor pada traffic Network Response
            page.on("response", lambda response: asyncio.create_task(
                process_gv_response(response, redis_client, session_id, page)
            ))

            logging.info(f"🌐 [{session_id}] Membuka halaman Google Voice...")
            # Arahkan ke URL tanpa membuka tab dobel
            await page.goto("https://voice.google.com/calls", wait_until="domcontentloaded")
            
            # Cek jika diperlukan login manual (untuk sesi baru)
            if "accounts.google.com" in page.url:
                logging.warning(f"⚠️ [{session_id}] Silakan selesaikan login akun Google di browser yang terbuka.")
                try:
                    await broadcast_admin(f"⚠️ <b>[{session_id}]</b> membutuhkan Login Manual di RDP! Silakan remote RDP Anda.")
                except Exception:
                    pass
                await page.wait_for_url("https://voice.google.com/**", timeout=0) 
                logging.info(f"✅ [{session_id}] Login sukses! Memuat ulang halaman untuk memicu WSS...")
                await page.reload(wait_until="domcontentloaded")

            logging.info(f"🕵️‍♂️ [{session_id}] Trunking Standby. Jalur WSS sedang dipantau.")
            
            # 🔥 PILLAR V2: WATCHDOG, SENGGOLAN FISIK, DAN REFRESH RUTIN 🔥
            check_count = 0
            while True:
                if state.get("KILLED"):
                    logging.info(f"🛑 [{session_id}] Menerima perintah penutupan armada. Menghentikan proses...")
                    break

                await asyncio.sleep(30) # Loop berjalan setiap 30 detik
                check_count += 1
                
                # 1. Watchdog: Jika 3 menit tanpa Ping/Pong Google, WSS berarti Zombie!
                if time.time() - state["LAST_ACTIVITY_TS"] > 180:
                    logging.error(f"🐕 [WATCHDOG {session_id}] Trunking koma selama 3 menit! Memicu setrum...")
                    asyncio.create_task(trigger_defibrillator("Watchdog Koma 3 Menit"))
                    state["LAST_ACTIVITY_TS"] = time.time() 

                # 2. Senggolan Fisik: Setiap 5 menit bawa tab ke depan agar ga di-throttle Windows
                if check_count % 10 == 0:
                    try:
                        await page.bring_to_front()
                    except Exception: pass

                # 3. Refresh Rutin: Setiap 30 Menit (Menggantikan loop 1800s lama)
                if check_count % 60 == 0:
                    logging.info(f"🔄 [{session_id}] Me-refresh halaman (Siklus Memory rutin)...")
                    try:
                        state["CACHED_PAYLOAD"] = None 
                        await page.reload(wait_until="domcontentloaded")
                    except PlaywrightError:
                        logging.warning(f"⚠️ [{session_id}] Tab tidak terdeteksi (mungkin di-close). Membatalkan siklus.")
                        break 

                # 4. Hard Restart: Setelah 12 Jam (Pembersih Leak RAM)
                if check_count >= 1440:
                    logging.warning(f"♻️ [{session_id}] [GARBAGE COLLECTION] Waktu Hard Restart Tiba! Menutup Chrome untuk membersihkan RAM...")
                    break
    finally:
        # 🔥 VACCINE 10055: Sapu bersih socket ke Redis agar Windows tidak kehabisan port! 🔥
        if not state.get("KILLED"):
            state["STATUS"] = "OFFLINE 🔴"
        logging.info(f"🧹 [{session_id}] Membersihkan Socket dan Background Task...")
        if state["REDIS_TASK"]:
            state["REDIS_TASK"].cancel()
        await redis_client.aclose()
        logging.info(f"🧹 [{session_id}] Pembersihan selesai. RAM dan Port kembali lega.")

# 🔥 KEEPALIVE WRAPPER (Bingkai Keabadian per Sesi) 🔥
async def keep_alive_harvester(session_id):
    # AUTO-CLEANUP: Sniper Zombie Chrome
    # Membunuh sisa-sisa Chrome yang nyangkut di profil ini SEBELUM script jalan
    logging.info(f"🧹 Mengecek dan membersihkan zombie Chrome untuk profil [{session_id}]...")
    try:
        cmd = f'wmic process where "name=\'chrome.exe\' and commandline like \'%gv_profile_{session_id}%\'" call terminate'
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    while not GLOBAL_STATE.get(session_id, {}).get("KILLED", False):
        try:
            await run_harvester(session_id)
            if GLOBAL_STATE.get(session_id, {}).get("KILLED", False):
                break
            logging.info(f"♻️ [{session_id}] Memulai ulang instance Browser baru...")
        except Exception as e:
            if GLOBAL_STATE.get(session_id, {}).get("KILLED", False):
                break
            GLOBAL_STATE[session_id]["STATUS"] = "CRASHED 🔴"
            logging.error(f"⚠️ [FATAL {session_id}] Terjadi kesalahan kritis: {e}. Mengulang dalam 10 detik...")
            try:
                await broadcast_admin(f"🚨 <b>[CRASH FATAL]</b>\nTrunking <b>{session_id}</b> meledak: <code>{e}</code>\nAuto-Restart dalam 10s.")
            except Exception:
                pass
            await asyncio.sleep(10)

# 🔥 PARSER INPUT CERDAS 🔥
def parse_sessions(input_str):
    sessions = []
    for part in input_str.split(','):
        part = part.strip()
        if not part: continue
        if '-' in part:
            try:
                # Ekstrak kata prefix dan angka (contoh: va1-va10 -> prefix="va", start=1, end=10)
                prefix_match = re.match(r"([a-zA-Z]+)(\d+)-[a-zA-Z]*(\d+)", part)
                if prefix_match:
                    prefix = prefix_match.group(1)
                    start = int(prefix_match.group(2))
                    end = int(prefix_match.group(3))
                    for i in range(start, end + 1):
                        sessions.append(f"{prefix}{i}")
                else:
                    sessions.append(part)
            except:
                sessions.append(part)
        else:
            sessions.append(part)
            
    # Menghapus duplikat dan mempertahankan urutan
    result = []
    for s in sessions:
        if s not in result:
            result.append(s)
    return result

async def main():
    print("="*60)
    print("🚀 TELPONY.COM TRUNKING COMMANDER (TELEGRAM CONTROL)")
    print("="*60)
    print("💡 Bisa ketik armada tunggal/ganda: va1 ATAU va1,va2,va3")
    print("💡 Bisa ketik rentang otomatis: va,va1-va10")
    
    # 🔥 ARSITEKTUR CLUSTER: Aktifkan Leader Poller dan Listener secara paralel
    leader_task = asyncio.create_task(telegram_leader_poller())
    ACTIVE_TASKS.add(leader_task)
    leader_task.add_done_callback(ACTIVE_TASKS.discard)
    
    listener_task = asyncio.create_task(redis_command_listener())
    ACTIVE_TASKS.add(listener_task)
    listener_task.add_done_callback(ACTIVE_TASKS.discard)
    
    # 🔥 FIX: Gunakan to_thread agar input() tidak memblokir bot Telegram di background
    session_input = await asyncio.to_thread(input, "\nMasukkan ID Trunking Awal (kosongkan lalu tekan Enter jika mau start via Telegram Bot): ")
    session_input = session_input.strip()
    
    if session_input:
        session_ids = parse_sessions(session_input)
        print(f"\n🚀 Menyiapkan peluncuran untuk {len(session_ids)} armada: {', '.join(session_ids)}\n")
        
        # Menjalankan seluruh armada secara paralel / serentak!
        for sid in session_ids:
            # 🔥 FIX: Strong Reference dan Jeda Peluncuran Terminal
            task = asyncio.create_task(keep_alive_harvester(sid))
            ACTIVE_TASKS.add(task)
            task.add_done_callback(ACTIVE_TASKS.discard)
            await asyncio.sleep(3) # Jeda 3 detik per Chrome
    else:
        print("\n⏳ Standby Mode. Silakan kirim perintah /add vaX melalui Telegram bot Anda.\n")

    # Menjaga Main Loop agar tetap hidup selamanya
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
            break
        except OSError as e:
            # 🔥 OS COOLDOWN SYSTEM (Penyembuh WinError 10055 tanpa restart RDP) 🔥
            if getattr(e, 'winerror', None) == 10055:
                print("\n🚨 [OS FATAL] Windows kehabisan Port Jaringan (WinError 10055)!")
                print("⏳ Script akan menunggu 60 detik agar Windows membersihkan cache jaringan secara otomatis...")
                time.sleep(60)
                print("🔄 OKE! Jaringan sudah lega. Mencoba menjalankan ulang Trunking...\n")
            else:
                raise e
        except KeyboardInterrupt:
            print("\n🛑 Sistem dihentikan dengan aman.")
            break
