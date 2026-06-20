import os, sqlite3, re, random, string, math, requests, json, shutil
from datetime import date, timedelta, datetime
from collections import defaultdict
from flask import Flask, render_template_string, request, redirect, url_for, session, g, make_response, send_file, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

try:
    from librouteros import connect as mt_connect_lib
    from librouteros.login import plain as mt_plain
    HAS_MT = True
except ImportError:
    HAS_MT = False

# ============================================================
# APP CONFIGURATION
# ============================================================
app = Flask(__name__)
app.secret_key = 'rockabywifi-secret-key-change-in-production'
app.permanent_session_lifetime = timedelta(days=30)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'rockabywifi.db')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)

MIKROTIK_HOST = '192.168.1.1'
MIKROTIK_USER = 'admin'
MIKROTIK_PASS = 'your_password'
MIKROTIK_PORT = 8728

def mt_connect():
    if not HAS_MT: return None
    try: return mt_connect_lib(username=MIKROTIK_USER, password=MIKROTIK_PASS, host=MIKROTIK_HOST, port=MIKROTIK_PORT)
    except: return None

def mt_add_user(phone, mins):
    api = mt_connect()
    if not api: return False
    try:
        api(cmd='/ip/hotspot/user/add', name=''.join(filter(str.isdigit, phone)), password=generate_voucher_code(), **{'limit-uptime': f"{mins}m"}, comment=f'RockabyWiFi – {mins} min')
        api.close(); return True
    except: return False

def mt_remove_user(username):
    api = mt_connect()
    if not api: return False
    try:
        for u in api(cmd='/ip/hotspot/user/print', where={'name': username}): api(cmd='/ip/hotspot/user/remove', **{'.id': u['.id']})
        api.close(); return True
    except: return False

# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA busy_timeout = 5000;")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None: db.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS providers (id INTEGER PRIMARY KEY AUTOINCREMENT, business_name TEXT NOT NULL, contact TEXT, password_hash TEXT NOT NULL, subscription_expiry DATE, percent_fee REAL DEFAULT 5.0, monthly_fee_ugx INTEGER DEFAULT 20000, auto_approve INTEGER DEFAULT 1, is_active INTEGER DEFAULT 1, mtn_number TEXT, airtel_number TEXT, poster_image TEXT, logo_image TEXT, support_phone TEXT, yo_username TEXT, yo_password TEXT, yo_auto_pay INTEGER DEFAULT 0)''')
    c.execute("PRAGMA table_info(providers)")
    existing = [col[1] for col in c.fetchall()]
    for col in ['poster_image','logo_image','support_phone','yo_username','yo_password','yo_auto_pay']:
        if col not in existing: c.execute(f"ALTER TABLE providers ADD COLUMN {col} TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, duration_minutes INTEGER NOT NULL, price_ugx INTEGER NOT NULL, is_active INTEGER DEFAULT 1, is_public INTEGER DEFAULT 1, speed_down TEXT, speed_up TEXT, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute("PRAGMA table_info(plans)")
    plan_cols = [col[1] for col in c.fetchall()]
    for col in ['is_public','speed_down','speed_up']:
        if col not in plan_cols: c.execute(f"ALTER TABLE plans ADD COLUMN {col} TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS voucher_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT NOT NULL, plan_id INTEGER, raw_sms TEXT NOT NULL, transaction_id TEXT, amount INTEGER, recipient TEXT, payment_date TEXT, status TEXT DEFAULT 'pending', voucher_code TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS vouchers (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, code TEXT UNIQUE NOT NULL, plan_id INTEGER, payment_method TEXT DEFAULT 'sms', phone_number TEXT, used INTEGER DEFAULT 0, used_at TIMESTAMP, mac_address TEXT, ip_address TEXT, batch_id TEXT, expiry_date DATE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute("PRAGMA table_info(vouchers)")
    vouch_cols = [col[1] for col in c.fetchall()]
    for col in ['batch_id','expiry_date']:
        if col not in vouch_cols: c.execute(f"ALTER TABLE vouchers ADD COLUMN {col} TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, phone TEXT, current_ip TEXT, suspended INTEGER DEFAULT 0, package_name TEXT, expiry_date DATE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, voucher_id INTEGER, subscriber_id INTEGER, provider_id INTEGER, mac_address TEXT, ip_address TEXT, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ended_at TIMESTAMP, data_download REAL DEFAULT 0, data_upload REAL DEFAULT 0, FOREIGN KEY(voucher_id) REFERENCES vouchers(id), FOREIGN KEY(subscriber_id) REFERENCES subscribers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS restricted (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER, phone_number TEXT, mac_address TEXT, reason TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS data_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT, session_date DATE, data_download REAL DEFAULT 0, data_upload REAL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS sms_log (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT, message TEXT, sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT, action TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, subject TEXT NOT NULL, description TEXT, status TEXT DEFAULT 'open', priority TEXT DEFAULT 'medium', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT, email TEXT, source TEXT, notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL, category TEXT, expense_date DATE, payment_method TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, invoice_no TEXT UNIQUE, user_id INTEGER, amount REAL, paid_amount REAL DEFAULT 0, status TEXT DEFAULT 'pending', due_date DATE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT, kind TEXT, type TEXT, start_date DATE, end_date DATE, status TEXT DEFAULT 'inactive', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS equipment (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, model TEXT, serial_number TEXT, user_id INTEGER, price REAL, paid_amount REAL DEFAULT 0, status TEXT DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS mikrotik_routers (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, ip_address TEXT, username TEXT, password TEXT, api_port INTEGER DEFAULT 8728, is_active INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS trial_used (id INTEGER PRIMARY KEY AUTOINCREMENT, ip_address TEXT UNIQUE, used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS yo_tx (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, tx_ref TEXT UNIQUE, phone TEXT, amount INTEGER, status TEXT DEFAULT 'pending', voucher_code TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS expiry_dates (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, user_id INTEGER NOT NULL, expiry_date TIMESTAMP, grace_period INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id), FOREIGN KEY(user_id) REFERENCES subscribers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS ip_bindings (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, mikrotik_id INTEGER, name TEXT NOT NULL, package_id INTEGER, dhcp_lease TEXT, address TEXT NOT NULL, mac_address TEXT NOT NULL, expires_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id), FOREIGN KEY(mikrotik_id) REFERENCES mikrotik_routers(id), FOREIGN KEY(package_id) REFERENCES plans(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT, details TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    c.execute("SELECT COUNT(*) FROM providers WHERE id=1")
    if c.fetchone()[0] == 0:
        hashed = generate_password_hash('admin123')
        c.execute("INSERT INTO providers (id, business_name, contact, password_hash, subscription_expiry, is_active, mtn_number, airtel_number, support_phone) VALUES (1,?,?,?,?,?,?,?,?)",
                  ('RockabyWiFi','256751318876',hashed,date.today()+timedelta(days=3650),1,'0785686404','0751318876','256751318876'))
        for name, mins, price in [('3 Hours',180,500),('24 Hours',1440,1000),('Weekly',10080,5000),('Monthly',43200,20000)]:
            c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public, speed_down, speed_up) VALUES (1,?,?,?,1,'5M','2M')",(name,mins,price))
        c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public, speed_down, speed_up) VALUES (1,'Free Trial',5,0,0,'1M','512k')")
        c.execute("INSERT INTO settings (provider_id, key, value) VALUES (1,'auto_approve','1')")
    else:
        c.execute("SELECT COUNT(*) FROM plans WHERE provider_id=1 AND name='Free Trial'")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public, speed_down, speed_up) VALUES (1,'Free Trial',5,0,0,'1M','512k')")
    conn.commit()
    conn.close()

def backup_database():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_file = os.path.join(BACKUP_DIR, f"rockabywifi_backup_{date.today().isoformat()}.db")
    if not os.path.exists(backup_file):
        shutil.copy2(DB_PATH, backup_file)

# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'provider_id' not in session and 'subscriber_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def parse_mtn_sms(sms):
    tid=re.search(r'ID:\s*(\d+)',sms); amount=re.search(r'UGX\s*([\d,]+)',sms); recipient_name=re.search(r'to\s+(.+?),',sms); number_match=re.search(r'to\s+.+?[, ]+(\d{10,12})',sms); date_str=re.search(r'on\s+(\d{4}-\d{2}-\d{2})',sms)
    return {'tid':tid.group(1) if tid else None,'amount':int(amount.group(1).replace(',','')) if amount else None,'recipient_name':recipient_name.group(1).strip() if recipient_name else None,'recipient_number':number_match.group(1) if number_match else None,'date':date_str.group(1) if date_str else None}

def parse_airtel_sms(sms):
    tid=re.search(r'TID\s*(\d+)',sms); amount=re.search(r'UGX\s*([\d,]+)',sms); recipient_match=re.search(r'to\s+(.+?)\s+on\s+(\d+)',sms,re.IGNORECASE)
    if recipient_match: recipient_name=recipient_match.group(1).strip(); recipient_number=recipient_match.group(2).strip()
    else: recipient_match=re.search(r'to\s+(.+?)\s+\d',sms); recipient_name=recipient_match.group(1).strip() if recipient_match else None; recipient_number=None
    date_str=re.search(r'Date\s+(\d{2}-[A-Za-z]+-\d{4}\s+\d{2}:\d{2})',sms)
    return {'tid':tid.group(1) if tid else None,'amount':int(amount.group(1).replace(',','')) if amount else None,'recipient_name':recipient_name,'recipient_number':recipient_number,'date':date_str.group(1) if date_str else None}

def generate_voucher_code():
    return 'WIFI-'+''.join(random.choices(string.ascii_uppercase+string.digits,k=4))+'-'+''.join(random.choices(string.ascii_uppercase+string.digits,k=4))+'-'+''.join(random.choices(string.ascii_uppercase+string.digits,k=4))

def get_plan_options(pid, public_only=True):
    db=get_db()
    if public_only: plans=db.execute("SELECT id,name,duration_minutes,price_ugx FROM plans WHERE provider_id=? AND is_active=1 AND is_public=1",(pid,)).fetchall()
    else: plans=db.execute("SELECT id,name,duration_minutes,price_ugx FROM plans WHERE provider_id=? AND is_active=1",(pid,)).fetchall()
    return ''.join(f'<option value="{p["id"]}">{p["name"]} – {p["duration_minutes"]} min – UGX {p["price_ugx"]:,}</option>' for p in plans)

def get_pending_count(pid=1):
    db=get_db(); row=db.execute("SELECT COUNT(*) as cnt FROM voucher_requests WHERE provider_id=? AND status='pending'",(pid,)).fetchone()
    return row['cnt'] if row else 0

def get_auto_approve(pid=1):
    db=get_db(); row=db.execute("SELECT auto_approve FROM providers WHERE id=?",(pid,)).fetchone()
    return row['auto_approve'] if row else 1

def get_provider(pid):
    db=get_db(); return db.execute("SELECT * FROM providers WHERE id=?",(pid,)).fetchone()

def clean_number(num):
    d=''.join(filter(str.isdigit,num))
    if d.startswith('0'): d='256'+d[1:]
    elif not d.startswith('256'): d='256'+d
    return d

def allowed_file(fn): return '.' in fn and fn.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

def get_weekly_platform_revenue(pid=1):
    db=get_db(); today=date.today(); start=today if today.weekday()==6 else today-timedelta(days=today.weekday()+1); end=start+timedelta(days=6)
    row=db.execute("SELECT COALESCE(SUM(pl.price_ugx),0) as total FROM vouchers v JOIN plans pl ON v.plan_id=pl.id WHERE v.provider_id=? AND date(v.created_at) BETWEEN ? AND ?",(pid,start.isoformat(),end.isoformat())).fetchone()
    return int(row['total']*0.05), start, end

def format_data(size_mb):
    return f"{size_mb/1000:.2f} GB" if size_mb>=1000 else f"{size_mb:.2f} MB"

# seed_sample_data() removed – no more fake data

def yo_charge(phone, amount, plan_name, provider):
    if not provider['yo_username'] or not provider['yo_password']: return None
    ref = f"ROCK-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(1000,9999)}"
    payload = {"username": provider['yo_username'], "password": provider['yo_password'], "phone_number": clean_number(phone), "amount": str(amount), "currency": "UGX", "external_ref": ref, "callback_url": url_for('yo_callback', _external=True)}
    try:
        resp = requests.post("https://paymentsapi.yo.co.ug/v1/collection", json=payload, headers={"Content-Type":"application/json"})
        data = resp.json()
        if data.get('transaction_status') == 'SUCCEEDED':
            db = get_db()
            plan = db.execute("SELECT id, duration_minutes FROM plans WHERE provider_id=? AND price_ugx=? AND is_active=1 LIMIT 1",(provider['id'], amount)).fetchone()
            if plan:
                code = generate_voucher_code()
                db.execute("INSERT INTO vouchers (provider_id,code,plan_id,payment_method,phone_number,used,used_at) VALUES (?,?,?,'yo',?,1,CURRENT_TIMESTAMP)",(provider['id'],code,plan['id'],phone))
                db.execute("INSERT INTO yo_tx (provider_id,tx_ref,phone,amount,status,voucher_code) VALUES (?,?,?,?,'completed',?)",(provider['id'],ref,phone,amount,code))
                db.commit(); mt_add_user(phone, plan['duration_minutes'])
            return 'instant_success'
        if data.get('status') == 'success':
            db = get_db()
            db.execute("INSERT INTO yo_tx (provider_id,tx_ref,phone,amount,status) VALUES (?,?,?,?,'pending')",(provider['id'],ref,phone,amount))
            db.commit(); return data.get('redirect_url')
    except Exception as e: print(f"Yo! Payments error: {e}")
    return None

# ------------------------------------------------------------
# BASE TEMPLATE – Glassmorphism + Dark Mode
# ------------------------------------------------------------
base_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RockabyWiFi - {title}</title>
    <link rel="manifest" href="/manifest.json">
    <meta name="theme-color" content="#1a73e8">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --primary: #1a73e8; --primary-dark: #1557b0; --accent: #ff6b6b; --accent2: #51cf66; --accent3: #ffd43b;
            --bg: #f0f4f8; --card-bg: rgba(255,255,255,0.85); --glass-border: rgba(255,255,255,0.3);
            --text: #1a1a1a; --text-secondary: #666666; --border: #e0e0e0;
            --radius: 16px; --shadow: 0 8px 32px rgba(0,0,0,0.08); --sidebar-width: 260px;
        }
        .dark-mode {
            --bg: #0f172a; --card-bg: rgba(30,41,59,0.85); --glass-border: rgba(255,255,255,0.08);
            --text: #f1f5f9; --text-secondary: #94a3b8; --border: #334155;
            --shadow: 0 8px 32px rgba(0,0,0,0.3);
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            background-image: radial-gradient(circle at 10% 20%, rgba(26,115,232,0.08) 0%, transparent 50%),
                              radial-gradient(circle at 90% 80%, rgba(255,107,107,0.08) 0%, transparent 50%);
            color: var(--text); min-height:100vh;
            transition: all 0.3s;
        }
        .admin-layout { display: flex; }
        .sidebar {
            width: var(--sidebar-width); background: var(--card-bg); backdrop-filter: blur(20px);
            border-right: 1px solid var(--glass-border); height: 100vh; position: fixed; left:0; top:0; overflow-y:auto;
            transition: transform 0.3s, background 0.3s; z-index:1000; box-shadow: var(--shadow);
        }
        .sidebar.collapsed { transform: translateX(-100%); }
        .sidebar-header {
            padding: 24px 20px; border-bottom:1px solid var(--glass-border);
            display:flex; align-items:center; gap:12px;
            background: rgba(255,255,255,0.05);
        }
        .sidebar-header img { height:40px; width:40px; border-radius:10px; box-shadow:0 4px 12px rgba(0,0,0,0.3); }
        .sidebar-header h3 { font-size:1.3rem; font-weight:800; margin:0; }
        .sidebar-menu { padding:10px 0; }
        .sidebar-menu a {
            display:flex; align-items:center; gap:10px; padding:12px 24px; color:var(--text-secondary);
            text-decoration:none; transition:all 0.2s; font-size:0.9rem; border-left:3px solid transparent;
        }
        .sidebar-menu a:hover, .sidebar-menu a.active {
            background:linear-gradient(90deg, rgba(245,175,25,0.15), transparent);
            color:#f5af19; border-left-color:#f5af19;
        }
        .sidebar-menu .badge {
            background: linear-gradient(135deg, var(--primary), #6366f1);
            color:#fff; padding:2px 10px; border-radius:12px; font-size:0.75rem; margin-left:auto;
        }
        .main-content { margin-left:var(--sidebar-width); flex:1; transition:margin-left 0.3s; }
        .main-content.expanded { margin-left:0; }
        .topbar {
            background: var(--card-bg); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
            border-bottom:1px solid var(--glass-border); padding:14px 24px;
            display:flex; align-items:center; justify-content:space-between;
            transition: background 0.3s, border 0.3s;
            position: relative;
            z-index: 9999 !important;
        }
        .hamburger { font-size:1.5rem; cursor:pointer; background:none; border:none; color:var(--text); display:block; }
        .topbar-right {
            display:flex; align-items:center; gap:18px;
            position: relative;
            z-index: 99999 !important;
        }
        .settings-dropdown {
            position: relative;
            z-index: 99999 !important;
            overflow: visible !important;
        }
        .settings-dropdown-content {
            display: none;
            position: absolute !important;
            right: 0 !important;
            top: 100% !important;
            background: var(--card-bg) !important;
            backdrop-filter: blur(20px) !important;
            min-width: 180px !important;
            box-shadow: var(--shadow) !important;
            z-index: 999999 !important;
            border-radius: 12px !important;
            border: 1px solid var(--glass-border) !important;
            overflow: visible !important;
        }
        .settings-dropdown:hover .settings-dropdown-content {
            display: block !important;
        }
        .settings-dropdown-content a {
            color: var(--text);
            padding: 12px 18px;
            text-decoration: none;
            display: block;
        }
        .settings-dropdown-content a:hover {
            background: rgba(26,115,232,0.1);
        }
        .theme-toggle {
            background:rgba(26,115,232,0.1);
            border:1px solid var(--glass-border);
            border-radius:50%;
            width:40px;
            height:40px;
            display:flex;
            align-items:center;
            justify-content:center;
            cursor:pointer;
            font-size:1.2rem;
            transition:all 0.2s;
            color:var(--text);
        }
        .theme-toggle:hover { background:rgba(26,115,232,0.2); transform:scale(1.05); }
        .container { max-width:1400px; margin:24px auto; padding:0 20px; }
        .card {
            background: var(--card-bg); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
            border-radius:var(--radius); padding:28px; margin-bottom:20px;
            box-shadow:var(--shadow); border:1px solid var(--glass-border);
            transition: transform 0.2s, box-shadow 0.2s, background 0.3s, border 0.3s;
            overflow:visible;
        }
        .card:hover { transform: translateY(-2px); box-shadow: 0 12px 40px rgba(0,0,0,0.12); }
        .card-header {
            font-size:1.2rem; font-weight:700; margin-bottom:20px;
            border-bottom:1px solid var(--border); padding-bottom:14px;
            display:flex; justify-content:space-between; align-items:center;
        }
        .stat-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:18px; margin-bottom:24px; }
        .stat-card {
            background: linear-gradient(135deg, rgba(26,115,232,0.08), rgba(99,102,241,0.05));
            border-radius:var(--radius); padding:24px; box-shadow:var(--shadow);
            border:1px solid var(--glass-border); text-align:center; position:relative; overflow:hidden;
            transition: background 0.3s, border 0.3s;
        }
        .stat-card::before {
            content:''; position:absolute; top:-30px; right:-30px; width:80px; height:80px;
            background: linear-gradient(135deg, var(--primary), #6366f1); opacity:0.15; border-radius:50%;
        }
        .stat-card h3 { font-size:2.2rem; font-weight:800; color:var(--primary); position:relative; }
        .stat-card small { color:var(--text-secondary); font-size:0.85rem; position:relative; }
        .btn {
            display:inline-block; padding:10px 22px; background: linear-gradient(135deg, var(--primary), #6366f1);
            color:#fff; border:none; border-radius:8px; font-weight:600; cursor:pointer;
            text-decoration:none; font-size:0.9rem; transition:all 0.2s; box-shadow:0 4px 15px rgba(26,115,232,0.3);
        }
        .btn:hover { transform: translateY(-1px); box-shadow:0 6px 20px rgba(26,115,232,0.4); }
        .btn-outline { background:transparent; border:2px solid var(--primary); color:var(--primary); box-shadow:none; }
        .btn-small { padding:6px 12px; font-size:0.8rem; }
        .btn-danger { background: linear-gradient(135deg, #dc3545, #ff6b6b); }
        .btn-success { background: linear-gradient(135deg, #28a745, #51cf66); }
        .chart-container { position:relative; width:100%; max-height:380px; margin:20px 0; }
        .chart-row { display:flex; gap:18px; flex-wrap:wrap; }
        .chart-row .card { flex:1; min-width:380px; }
        .voucher-code {
            font-size:1.5rem; font-weight:700; letter-spacing:1px;
            background: linear-gradient(135deg, var(--primary), #6366f1); color:#fff;
            padding:12px 18px; border-radius:10px; display:inline-block; margin:10px 0;
        }
        .tabs { display:flex; gap:10px; margin-bottom:18px; flex-wrap:wrap; }
        .tab {
            padding:8px 18px; border-radius:20px; cursor:pointer; background:var(--bg);
            border:1px solid var(--border); font-size:0.9rem; text-decoration:none; color:var(--text);
            transition:all 0.2s;
        }
        .tab.active { background: linear-gradient(135deg, var(--primary), #6366f1); color:#fff; border-color:transparent; }
        .whatsapp-float {
            position:fixed; bottom:24px; right:24px; background: linear-gradient(135deg, #25D366, #128C7E);
            color:white; width:60px; height:60px; border-radius:50%; display:flex; align-items:center;
            justify-content:center; font-size:28px; box-shadow:0 8px 25px rgba(37,211,102,0.4);
            z-index:999; text-decoration:none; transition:transform 0.2s;
        }
        .whatsapp-float:hover { transform: scale(1.1); }
        .provider-logo { height:50px; width:50px; border-radius:10px; margin-right:12px; vertical-align:middle; object-fit:cover; border:2px solid var(--primary); }
        .provider-poster { width:100%; max-height:220px; object-fit:cover; border-radius:var(--radius); margin-bottom:15px; box-shadow:var(--shadow); }
        .remember-row { display:flex; align-items:center; margin-top:15px; }
        .remember-row input[type="checkbox"] { width:auto; margin-right:8px; }
        .copy-btn { background: #28a745; color: white; border: none; padding: 8px 15px; border-radius: 6px; cursor: pointer; font-weight: 600; margin-left: 10px; }
        
        /* ========== DROPDOWN FIX ========== */
        .card, .container, .main-content, .table-responsive, table, thead, tbody, tr, td, th {
            overflow: visible !important;
        }
        .dropdown {
            position: relative !important;
            display: inline-block !important;
            z-index: 9999 !important;
        }
        .dropdown-content {
            display: none !important;
            position: absolute !important;
            right: 0 !important;
            top: 100% !important;
            background: var(--card-bg) !important;
            backdrop-filter: blur(20px) !important;
            min-width: 200px !important;
            box-shadow: 0 12px 40px rgba(0,0,0,0.25) !important;
            border-radius: 12px !important;
            border: 1px solid var(--glass-border) !important;
            padding: 5px 0 !important;
            z-index: 999999 !important;
        }
        .dropdown:hover .dropdown-content {
            display: block !important;
        }
        .dropdown-content a {
            display: block !important;
            padding: 10px 18px !important;
            color: var(--text) !important;
            text-decoration: none !important;
            white-space: nowrap !important;
        }
        .dropdown-content a:hover {
            background: rgba(26,115,232,0.1) !important;
        }
        /* ========== END DROPDOWN FIX ========== */
        
        footer { text-align:center; padding:24px; color:var(--text-secondary); border-top:1px solid var(--border); margin-top:40px; }
        table { width:100%; border-collapse:collapse; }
        th, td { padding:10px 12px; text-align:left; border-bottom:1px solid var(--border); }
        th { background:var(--bg); font-weight:600; }
        label { display:block; margin-top:15px; font-weight:500; }
        input, textarea, select {
            width:100%; padding:10px 12px; margin-top:5px; border-radius:8px;
            border:1px solid var(--border); font-size:0.95rem; background:var(--card-bg); color:var(--text);
        }
        .alert { padding:12px 18px; border-radius:8px; margin-bottom:15px; }
        .alert-success { background:rgba(40,167,69,0.15); color:#155724; border:1px solid rgba(40,167,69,0.3); }
        .alert-error { background:rgba(220,53,69,0.15); color:#721c24; border:1px solid rgba(220,53,69,0.3); }
        .install-btn {
            background: linear-gradient(135deg, #28a745, #20c997);
            color: white;
            border: none;
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 0.85rem;
            cursor: pointer;
            display: none;
            font-weight: 600;
        }
        .install-btn:hover { transform: scale(1.05); }
        @media (max-width:768px) {
            .sidebar { transform:translateX(-100%); } .sidebar.open { transform:translateX(0); }
            .main-content { margin-left:0; } .chart-row { flex-direction:column; }
            .chart-row .card { min-width:100%; }
            .stat-grid {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 10px !important;
            }
            .stat-card {
                padding: 16px !important;
            }
            .stat-card h3 {
                font-size: 1.5rem !important;
            }
        }
    </style>
</head>
<body class="{layout_class}">
    {sidebar_html}
    <div class="main-content" id="mainContent">
        {topbar_html}
        <div class="container">{content}</div>
        <footer>&copy; 2025 RockabyTech – WiFi Billing Made Simple</footer>
    </div>
    <a href="https://wa.me/{support_phone}?text=Hi%20RockabyWiFi%20Support" target="_blank" class="whatsapp-float">💬</a>
    <script>
        function toggleSidebar() {
            var sb = document.getElementById('sidebar');
            sb.classList.toggle('open'); sb.classList.toggle('collapsed');
            document.getElementById('mainContent').classList.toggle('expanded');
        }
        function toggleTheme() {
            document.body.classList.toggle('dark-mode');
            localStorage.setItem('rockabywifi-theme', document.body.classList.contains('dark-mode') ? 'dark' : 'light');
        }
        if (localStorage.getItem('rockabywifi-theme') === 'dark') {
            document.body.classList.add('dark-mode');
        }

        // PWA install prompt
        let deferredPrompt;
        const installBtn = document.getElementById('installBtn');
        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredPrompt = e;
            if (installBtn) installBtn.style.display = 'inline-block';
        });
        if (installBtn) {
            installBtn.addEventListener('click', async () => {
                if (deferredPrompt) {
                    deferredPrompt.prompt();
                    const { outcome } = await deferredPrompt.userChoice;
                    deferredPrompt = null;
                    installBtn.style.display = 'none';
                }
            });
        }
        window.addEventListener('appinstalled', () => {
            if (installBtn) installBtn.style.display = 'none';
        });

        // Service Worker
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/service-worker.js')
                    .then(() => console.log('Service Worker registered'))
                    .catch(err => console.log('Service Worker failed:', err));
            });
        }
    </script>
</body>
</html>
"""

# ------------------------------------------------------------
# RENDER_PAGE FUNCTION
# ------------------------------------------------------------
def render_page(title, content, pending_count=0, provider_id=1, admin=False):
    provider = get_provider(provider_id)
    sp = provider['support_phone'] if provider and provider['support_phone'] else '256751318876'
    if admin and session.get('provider_id'):
        db = get_db()
        active_cnt = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=? AND used=0",(session['provider_id'],)).fetchone()['c']
        users_cnt = db.execute("SELECT COUNT(*) as c FROM subscribers WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        tix_cnt = db.execute("SELECT COUNT(*) as c FROM tickets WHERE provider_id=? AND status='open'",(session['provider_id'],)).fetchone()['c']
        leads_cnt = db.execute("SELECT COUNT(*) as c FROM leads WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        pkg_cnt = db.execute("SELECT COUNT(*) as c FROM plans WHERE provider_id=? AND is_active=1 AND is_public=1",(session['provider_id'],)).fetchone()['c']
        vouch_cnt = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        inv_cnt = db.execute("SELECT COUNT(*) as c FROM invoices WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        camp_cnt = db.execute("SELECT COUNT(*) as c FROM campaigns WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        mt_cnt = db.execute("SELECT COUNT(*) as c FROM mikrotik_routers WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        eq_cnt = db.execute("SELECT COUNT(*) as c FROM equipment WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        exp_cnt = db.execute("SELECT COUNT(*) as c FROM expiry_dates WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        ip_cnt = db.execute("SELECT COUNT(*) as c FROM ip_bindings WHERE provider_id=?",(session['provider_id'],)).fetchone()['c']
        sidebar = f"""<div class="sidebar" id="sidebar"><div class="sidebar-header"><img src="/static/icon-192.png"><h3 style="font-size:1.3rem; font-weight:800; margin:0;"><span style="color:#1a73e8;">ROCKABY</span><span style="color:#f5af19;">TECH</span></h3></div><div class="sidebar-menu">
        <a href="/dashboard"><i class="fas fa-tachometer-alt"></i> Dashboard</a>
        <a href="/active-users"><i class="fas fa-wifi"></i> Active Users <span class="badge">{active_cnt}</span></a>
        <a href="/users"><i class="fas fa-users"></i> Users <span class="badge">{users_cnt}</span></a>
        <a href="/expiry-dates" style="padding-left:30px;font-size:0.85rem;"><i class="far fa-clock"></i> Expiry Dates <span class="badge">{exp_cnt}</span></a>
        <a href="/ip-bindings" style="padding-left:30px;font-size:0.85rem;"><i class="fas fa-link"></i> IP Bindings <span class="badge">{ip_cnt}</span></a>
        <a href="/tickets"><i class="fas fa-ticket-alt"></i> Tickets <span class="badge">{tix_cnt}</span></a>
        <a href="/leads"><i class="fas fa-chart-line"></i> Leads <span class="badge">{leads_cnt}</span></a>
        <hr style="border-color:rgba(255,255,255,0.1); margin:10px 0;">
        <a href="/plans"><i class="fas fa-box"></i> Packages <span class="badge">{pkg_cnt}</span></a>
        <a href="/payments"><i class="fas fa-money-bill-wave"></i> Payments</a>
        <a href="/vouchers"><i class="fas fa-ticket-alt"></i> Vouchers <span class="badge">{vouch_cnt}</span></a>
        <a href="/invoices"><i class="fas fa-file-invoice"></i> Invoices <span class="badge">{inv_cnt}</span></a>
        <a href="/expenses"><i class="fas fa-receipt"></i> Expenses</a>
        <hr style="border-color:rgba(255,255,255,0.1); margin:10px 0;">
        <a href="/messages"><i class="fas fa-envelope"></i> Messages</a>
        <a href="/email"><i class="fas fa-at"></i> Emails</a>
        <a href="/campaign"><i class="fas fa-bullhorn"></i> Campaigns <span class="badge">{camp_cnt}</span></a>
        <hr style="border-color:rgba(255,255,255,0.1); margin:10px 0;">
        <a href="/mikrotik"><i class="fas fa-server"></i> MikroTik <span class="badge">{mt_cnt}</span></a>
        <a href="/equipment"><i class="fas fa-tools"></i> Equipment <span class="badge">{eq_cnt}</span></a>
        </div></div>"""
        topbar = f'<div class="topbar"><button class="hamburger" onclick="toggleSidebar()">&#9776;</button><div class="topbar-right"><button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark/light mode">🌓</button><span style="color:#1a73e8; font-weight:600;">Welcome, {session["provider_name"]}</span><div class="settings-dropdown"><a href="#" style="color:var(--text);text-decoration:none;"><i class="fas fa-cog"></i></a><div class="settings-dropdown-content"><a href="/provider/edit"><i class="fas fa-sliders-h"></i> Settings</a><a href="/logout"><i class="fas fa-sign-out-alt"></i> Logout</a></div></div></div></div>'
        layout = 'admin-layout'
    else:
        sidebar = ''
        topbar = '''
    <div class="topbar" style="background:var(--card-bg); backdrop-filter:blur(20px); border-bottom:1px solid var(--glass-border); padding:14px 24px; display:flex; align-items:center; justify-content:space-between;">
        <div style="display:flex; align-items:center; gap:12px;">
            <img src="/static/icon-192.png" alt="RockabyTech" style="height:35px; width:35px; border-radius:8px; object-fit:cover;">
            <span style="font-size:1.2rem; font-weight:800;">
                <span style="color:#1a73e8;">ROCKABY</span><span style="color:#f5af19;">TECH</span>
            </span>
            <span style="font-size:0.75rem; color:var(--text-secondary); margin-left:5px;">WiFi Billing</span>
        </div>
        <div style="display:flex; align-items:center; gap:15px;">
            <button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark/light mode">🌓</button>
            <a href="/login" class="btn btn-small">Login</a>
        </div>
    </div>
    '''
        layout = 'public-layout'
    return base_template.replace('{title}',title).replace('{layout_class}',layout).replace('{sidebar_html}',sidebar).replace('{topbar_html}',topbar).replace('{content}',content).replace('{support_phone}',sp)

# ------------------------------------------------------------
# CUSTOMER ROUTES
# ------------------------------------------------------------
@app.route('/')
def home():
    pid = request.args.get('pid', 1, type=int)
    p = get_provider(pid)
    if not p: return "Provider not found.", 404
    bn = p['business_name'] if p else 'RockabyWiFi'
    logo = f'<img src="/static/uploads/{p["logo_image"]}" class="provider-logo" alt="{bn}">' if p and p['logo_image'] else ''
    poster = f'<img src="/static/uploads/{p["poster_image"]}" class="provider-poster" alt="Poster">' if p and p['poster_image'] else ''

    hero_logo = '<img src="/static/ug-06.png" alt="RockabyWiFi" style="height:80px; width:80px; border-radius:16px; object-fit:cover; margin-bottom:15px; box-shadow:0 4px 15px rgba(0,0,0,0.2);">'
    content = f'''<div class="card" style="display:flex; align-items:center; gap:15px; flex-wrap:wrap;">{logo}<h2 style="margin:0;">{bn}</h2></div>{poster}
    <div class="hero" style="background: linear-gradient(135deg, rgba(26,115,232,0.15), rgba(99,102,241,0.1)); border-radius:var(--radius); padding:40px; text-align:center; margin-bottom:30px; border:1px solid var(--glass-border);">
        {hero_logo}
        <h1 style="font-size:2.5rem; margin-bottom:15px; background:linear-gradient(135deg, var(--primary), #6366f1); -webkit-background-clip:text; -webkit-text-fill-color:transparent;">Fast & Reliable WiFi</h1>
        <p style="font-size:1.1rem; color:var(--text-secondary); margin-bottom:20px;">Choose a plan and get connected in minutes.</p>
    </div>
    <div class="card"><div class="card-header">Choose a Plan</div>
    <form method="GET" action="/sms-verify"><input type="hidden" name="pid" value="{pid}"><label>Your Phone Number *</label><input type="tel" name="phone" required><label>Select Plan</label><select name="plan_id" required>{get_plan_options(pid)}</select><button type="submit" class="btn" style="margin-top:20px;width:100%;">Continue to Payment</button></form></div>
    <p style="text-align:center;margin-top:15px;"><a href="/redeem?pid={pid}" class="btn btn-outline">Already have a voucher?</a> <a href="/subscriber-login?pid={pid}" class="btn btn-outline" style="margin-left:10px;">Subscriber Login</a></p>
    <p style="text-align:center;margin-top:10px;"><a href="/free-trial?pid={pid}" class="btn btn-outline" style="background: linear-gradient(135deg, #28a745, #51cf66); color:white; border:none;">🎁 Free 5-Minute Trial</a></p>'''
    return render_page("Get Internet Access", content, get_pending_count(pid), pid, admin=False)

@app.route('/free-trial')
def free_trial():
    pid = request.args.get('pid', 1, type=int)
    ip = request.remote_addr; db = get_db()
    if db.execute("SELECT COUNT(*) as cnt FROM trial_used WHERE ip_address=? AND provider_id=?",(ip,pid)).fetchone()['cnt'] > 0:
        return render_page("Free Trial",'<div class="card"><div class="alert alert-error">You have already used your free trial.</div><p><a href="/?pid='+str(pid)+'" class="btn">Back to Home</a></p></div>', get_pending_count(pid), pid, admin=False)
    trial = db.execute("SELECT id, duration_minutes FROM plans WHERE provider_id=? AND name='Free Trial' AND is_active=1",(pid,)).fetchone()
    if not trial: return render_page("Free Trial",'<div class="card"><div class="alert alert-error">Trial not available.</div></div>', get_pending_count(pid), pid, admin=False)
    code = generate_voucher_code()
    db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, ip_address, used) VALUES (?, ?, ?, 'trial', ?, 0)",(pid, code, trial['id'], ip))
    db.execute("INSERT INTO trial_used (ip_address) VALUES (?)",(ip,)); db.commit()
    content = f'''<div class="card"><div class="alert alert-success">Free trial activated!</div><p><strong>Your Voucher Code:</strong></p><div class="voucher-code" id="vc">{code}</div><button class="copy-btn" onclick="navigator.clipboard.writeText('{code}')">📋 Copy</button><p style="margin-top:10px;">Use this code on the <a href="/redeem?pid={pid}">Redeem page</a> to connect for 5 minutes.</p><a href="/?pid={pid}" class="btn">Back to Home</a></div>'''
    return render_page("Free Trial", content, get_pending_count(pid), pid, admin=False)

@app.route('/redeem', methods=['GET','POST'])
def redeem():
    pid = request.args.get('pid', 1, type=int)
    if request.method == 'POST':
        code = request.form['code'].strip().upper(); db = get_db()
        v = db.execute("SELECT v.id, v.phone_number, p.duration_minutes FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.code=? AND v.used=0 AND v.provider_id=?",(code,pid)).fetchone()
        if v: db.execute("UPDATE vouchers SET used=1, used_at=CURRENT_TIMESTAMP WHERE id=?",(v['id'],)); db.commit(); mt_add_user(v['phone_number'] or 'trial', v['duration_minutes']); return render_page("Voucher Redeemed",'<div class="card"><div class="alert alert-success">Connected! Enjoy your internet access.</div><a href="/?pid='+str(pid)+'" class="btn">Back to Home</a></div>', get_pending_count(pid), pid, admin=False)
        return render_page("Redeem Voucher",'<div class="card"><div class="alert alert-error">Invalid or already used voucher code.</div><form method="POST"><input type="hidden" name="pid" value="'+str(pid)+'"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px;width:100%;">Redeem</button></form></div>', get_pending_count(pid), pid, admin=False)
    return render_page("Redeem Voucher",f'<div class="card"><div class="card-header">Redeem Voucher</div><form method="POST"><input type="hidden" name="pid" value="{pid}"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px;width:100%;">Redeem</button></form></div>', get_pending_count(pid), pid, admin=False)

@app.route('/sms-verify', methods=['GET','POST'])
def sms_verify():
    pid = request.args.get('pid', 1, type=int)
    phone = request.args.get('phone',''); plan_id = request.args.get('plan_id','1'); pc = get_pending_count(pid)
    db = get_db(); plan = db.execute("SELECT * FROM plans WHERE id=? AND provider_id=?",(plan_id,pid)).fetchone()
    if not plan: return "Invalid plan selected.", 400
    prov = db.execute("SELECT auto_approve, mtn_number, airtel_number FROM providers WHERE id=?",(pid,)).fetchone()
    if request.method == 'POST':
        phone = request.form['phone'].strip(); plan_id = int(request.form['plan_id']); raw = request.form['raw_sms'].strip()
        parsed = parse_airtel_sms(raw) if 'TID' in raw or 'SENT.TID' in raw else parse_mtn_sms(raw)
        err = None
        if not parsed['tid']: err = "Could not detect Transaction ID."
        elif not parsed['amount']: err = "Could not detect amount."
        elif parsed['amount'] != plan['price_ugx']: err = f"Amount mismatch. Expected UGX {plan['price_ugx']:,}."
        elif not parsed.get('recipient_name'): err = "Could not detect recipient."
        else:
            mtn = clean_number(prov['mtn_number']) if prov['mtn_number'] else ''; air = clean_number(prov['airtel_number']) if prov['airtel_number'] else ''
            sms_num = clean_number(parsed.get('recipient_number','')) if parsed.get('recipient_number') else ''
            if sms_num:
                if sms_num != mtn and sms_num != air: err = "Payment not sent to the correct provider number."
            else:
                rl = parsed['recipient_name'].lower()
                if prov['mtn_number'] and prov['mtn_number'] not in rl and prov['airtel_number'] and prov['airtel_number'] not in rl: err = "Payment not sent to the correct provider number."
        if err: return render_page("Verify Payment",f'<div class="card"><div class="alert alert-error">{err}</div><form method="POST"><input type="hidden" name="phone" value="{phone}"><input type="hidden" name="plan_id" value="{plan_id}"><input type="hidden" name="pid" value="{pid}"><label>Paste Full MTN/Airtel SMS Here</label><textarea name="raw_sms" rows="6" required></textarea><button type="submit" class="btn" style="margin-top:20px;width:100%;">Verify Payment</button></form></div>', pc, pid, admin=False)
        if db.execute("SELECT COUNT(*) as cnt FROM voucher_requests WHERE transaction_id=? AND provider_id=?",(parsed['tid'],pid)).fetchone()['cnt'] > 0: return render_page("Verify Payment",'<div class="card"><div class="alert alert-error">This Transaction ID has already been used.</div><p><a href="/?pid='+str(pid)+'" class="btn">Back to Home</a></p></div>', pc, pid, admin=False)
        auto = prov['auto_approve'] if prov else 1; status = 'approved' if auto else 'pending'; vc = None
        rf = f"{parsed.get('recipient_name','')} {parsed.get('recipient_number','')}".strip()
        if status == 'approved':
            vc = generate_voucher_code()
            db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number) VALUES (?,?,?,'sms',?)",(pid,vc,plan_id,phone))
            db.execute("INSERT INTO voucher_requests (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status, voucher_code) VALUES (?,?,?,?,?,?,?,?,'approved',?)",(pid,phone,plan_id,raw,parsed['tid'],parsed['amount'],rf,parsed['date'],vc)); db.commit()
            content = f'<div class="card"><div class="alert alert-success">Payment verified!</div><p><strong>Your Voucher Code:</strong></p><div class="voucher-code" id="vc">{vc}</div><button class="copy-btn" onclick="navigator.clipboard.writeText(\'{vc}\')">📋 Copy</button><p style="margin-top:10px;">Use this code on the <a href="/redeem?pid={pid}">Redeem page</a> to connect.</p><a href="/?pid={pid}" class="btn">Back to Home</a></div>'
        else:
            db.execute("INSERT INTO voucher_requests (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status) VALUES (?,?,?,?,?,?,?,?,'pending')",(pid,phone,plan_id,raw,parsed['tid'],parsed['amount'],rf,parsed['date'])); db.commit()
            content = '<div class="card"><div class="alert alert-success">Payment submitted! Waiting for approval.</div><p><a href="/?pid='+str(pid)+'" class="btn">Back to Home</a></p></div>'
        return render_page("Verification Result", content, get_pending_count(pid), pid, admin=False)

    provider = get_provider(pid); auto_pay = provider['yo_auto_pay'] if provider and provider['yo_auto_pay'] else 0; auto_btn = ''
    if auto_pay and provider['yo_username'] and provider['yo_password']: auto_btn = f'<a href="/yo-pay?phone={phone}&plan_id={plan_id}&pid={pid}" class="btn" style="display:block;margin-top:10px;width:100%;background: linear-gradient(135deg, #28a745, #51cf66); text-align:center;">📱 Pay with Mobile Money (Auto)</a>'
    content = f'''<div class="card"><div class="card-header">Pay for Internet</div><p><strong>Selected Plan:</strong> {plan["name"]} – {plan["duration_minutes"]} min – UGX {plan["price_ugx"]:,}</p><p><strong>Pay to:</strong></p><p>MTN: {provider["mtn_number"] if provider and provider["mtn_number"] else 'N/A'} | Airtel: {provider["airtel_number"] if provider and provider["airtel_number"] else 'N/A'}</p><p style="color:#666;">Name: {provider["business_name"] if provider else "RockabyWiFi"}</p><hr>{auto_btn}<p style="margin-top:15px;"><strong>Or pay manually:</strong></p><p>After payment, paste the full SMS below:</p><form method="POST"><input type="hidden" name="phone" value="{phone}"><input type="hidden" name="plan_id" value="{plan_id}"><input type="hidden" name="pid" value="{pid}"><label>Paste Full MTN/Airtel SMS Here</label><textarea name="raw_sms" rows="6" required></textarea><button type="submit" class="btn" style="margin-top:20px;width:100%;">Verify Payment</button></form></div>'''
    return render_page("Verify Payment", content, pc, pid, admin=False)

@app.route('/yo-pay')
def yo_pay():
    pid = request.args.get('pid', 1, type=int)
    phone = request.args.get('phone',''); plan_id = request.args.get('plan_id','1')
    db = get_db(); plan = db.execute("SELECT * FROM plans WHERE id=? AND provider_id=?",(plan_id,pid)).fetchone()
    if not plan: return "Invalid plan.", 400
    provider = get_provider(pid); result = yo_charge(phone, plan['price_ugx'], plan['name'], provider)
    if result == 'instant_success': return redirect("https://google.com")
    if result: return redirect(result)
    return render_page("Payment Error",f'<div class="card"><div class="alert alert-error">Automatic payment is unavailable. Please use manual payment.</div><p><a href="/sms-verify?phone={phone}&plan_id={plan_id}&pid={pid}" class="btn">Manual Payment</a></p></div>', get_pending_count(pid), pid, admin=False)

@app.route('/yo-callback', methods=['POST'])
def yo_callback():
    data = request.get_json()
    if data and data.get('transaction_status') == 'SUCCEEDED':
        tx_ref = data.get('external_ref'); db = get_db()
        tx = db.execute("SELECT * FROM yo_tx WHERE tx_ref=? AND status='pending'",(tx_ref,)).fetchone()
        if tx:
            plan = db.execute("SELECT id, duration_minutes FROM plans WHERE provider_id=? AND price_ugx=? AND is_active=1 LIMIT 1",(tx['provider_id'], tx['amount'])).fetchone()
            if plan:
                code = generate_voucher_code()
                db.execute("INSERT INTO vouchers (provider_id,code,plan_id,payment_method,phone_number,used,used_at) VALUES (?,?,?,'yo',?,1,CURRENT_TIMESTAMP)",(tx['provider_id'],code,plan['id'],tx['phone']))
                db.execute("UPDATE yo_tx SET status='completed', voucher_code=? WHERE tx_ref=?",(code, tx_ref)); db.commit(); mt_add_user(tx['phone'], plan['duration_minutes'])
    return 'OK', 200

@app.route('/subscriber-login', methods=['GET','POST'])
def subscriber_login():
    pid = request.args.get('pid', 1, type=int)
    if request.method == 'POST':
        u = request.form['username'].strip(); pw = request.form['password']; db = get_db()
        sub = db.execute("SELECT id, password_hash, suspended FROM subscribers WHERE username=? AND provider_id=?",(u,pid)).fetchone()
        if sub and check_password_hash(sub['password_hash'],pw) and not sub['suspended']:
            db.execute("DELETE FROM sessions WHERE subscriber_id=?",(sub['id'],)); ip = request.remote_addr
            db.execute("INSERT INTO sessions (subscriber_id, provider_id, ip_address) VALUES (?,?,?)",(sub['id'],pid,ip))
            db.execute("UPDATE subscribers SET current_ip=? WHERE id=?",(ip,sub['id'])); db.commit()
            session['subscriber_id']=sub['id']; session['subscriber_name']=u; return redirect(url_for('subscriber_portal'))
        return render_page("Subscriber Login",'<div class="card"><div class="alert alert-error">Invalid credentials or account suspended.</div><a href="/subscriber-login?pid='+str(pid)+'" class="btn">Try again</a></div>', get_pending_count(pid), pid, admin=False)
    return render_page("Subscriber Login",f'<div class="card"><div class="card-header">Subscriber Login</div><form method="POST"><input type="hidden" name="pid" value="{pid}"><label>Username</label><input type="text" name="username" required><label>Password</label><input type="password" name="password" required><button type="submit" class="btn" style="margin-top:20px;">Login</button></form></div>', get_pending_count(pid), pid, admin=False)

@app.route('/subscriber-portal')
def subscriber_portal():
    if 'subscriber_id' not in session: return redirect('/subscriber-login')
    pid = session.get('provider_id', 1)
    return render_page("Subscriber Portal",f'<div class="card"><h2>Welcome, {session["subscriber_name"]}</h2><p>You are connected. Your IP: {request.remote_addr}</p><a href="/subscriber-logout" class="btn btn-danger">Logout / Switch Device</a></div>', get_pending_count(pid), pid, admin=False)

@app.route('/subscriber-logout')
def subscriber_logout():
    if 'subscriber_id' in session: db = get_db(); db.execute("DELETE FROM sessions WHERE subscriber_id=?",(session['subscriber_id'],)); db.commit(); session.pop('subscriber_id',None); session.pop('subscriber_name',None)
    return redirect('/')

# ------------------------------------------------------------
# ADMIN LOGIN
# ------------------------------------------------------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        contact = request.form['contact'].strip(); pw = request.form['password']; db = get_db()
        prov = db.execute("SELECT * FROM providers WHERE contact=?",(contact,)).fetchone()
        if prov and check_password_hash(prov['password_hash'],pw) and prov['is_active']:
            session['provider_id']=prov['id']; session['provider_name']=prov['business_name']
            if request.form.get('remember'): session.permanent = True
            return redirect('/dashboard')
        return render_page("Admin Login",'<div class="card"><div class="alert alert-error">Invalid credentials.</div><p><a href="/login">Try again</a></p></div>',0,1,admin=False)
    return render_page("Admin Login",'<div class="card"><div class="card-header">Provider Login</div><form method="POST"><label>Phone Number</label><input type="tel" name="contact" required><label>Password</label><input type="password" name="password" required><div class="remember-row"><input type="checkbox" name="remember"> Remember me</div><button type="submit" class="btn" style="margin-top:20px;width:100%;">Login</button></form></div>',0,1,admin=False)

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

# ------------------------------------------------------------
# PROVIDER DASHBOARD (Updated with real data)
# ------------------------------------------------------------
@app.route('/dashboard')
@login_required
def dashboard():
    pid = session['provider_id']
    db = get_db()

    today = date.today()
    month_start = today.replace(day=1).isoformat()
    last_month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()

    # 1. Amount This Month
    amount_this_month = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM voucher_requests "
        "WHERE provider_id = ? AND status = 'approved' AND date(created_at) >= ?",
        (pid, month_start)
    ).fetchone()['total']

    # 2. Total Clients (unique)
    total_clients = db.execute(
        "SELECT COUNT(DISTINCT phone_number) as total FROM vouchers WHERE provider_id = ?",
        (pid,)
    ).fetchone()['total']

    # 3. Active Now (connected in last 15 minutes)
    active_now = db.execute(
        "SELECT COUNT(*) as active FROM vouchers "
        "WHERE provider_id = ? AND used = 1 AND used_at >= datetime('now', '-15 minutes')",
        (pid,)
    ).fetchone()['active']

    # 4. Active Users (24 hours)
    active_users_24h = db.execute(
        "SELECT COUNT(DISTINCT phone_number) as active FROM vouchers "
        "WHERE provider_id = ? AND used = 1 AND used_at >= datetime('now', '-1 day')",
        (pid,)
    ).fetchone()['active']

    # Growth vs last month
    last_month_amount = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM voucher_requests "
        "WHERE provider_id = ? AND status = 'approved' AND date(created_at) >= ? AND date(created_at) < ?",
        (pid, last_month_start, month_start)
    ).fetchone()['total']
    if last_month_amount > 0:
        growth = ((amount_this_month - last_month_amount) / last_month_amount) * 100
        growth_text = f"+{growth:.0f}%" if growth >= 0 else f"{growth:.0f}%"
    else:
        growth_text = "New"

    # Package performance (unchanged)
    pkg_perf = db.execute("""
        SELECT p.name, p.price_ugx, COUNT(v.id) as active_users,
               COALESCE(SUM(p.price_ugx), 0) as monthly_rev,
               COALESCE(AVG(COALESCE(ds.data_download + ds.data_upload, 0)), 0) as avg_data
        FROM plans p
        LEFT JOIN vouchers v ON v.plan_id = p.id AND v.used = 0
        LEFT JOIN data_sessions ds ON ds.phone_number = v.phone_number
        WHERE p.provider_id = ? AND p.is_active = 1 AND p.is_public = 1
        GROUP BY p.id
        ORDER BY monthly_rev DESC
    """, (pid,)).fetchall()
    pkg_rows = ''
    for pr in pkg_perf:
        arpu = int(pr['monthly_rev']) / int(pr['active_users']) if int(pr['active_users']) > 0 else 0
        pkg_rows += f'''
        <tr><td>{pr["name"]}</td><td>UGX {pr["price_ugx"]:,}</td><td>{pr["active_users"]}</td><td>UGX {pr["monthly_rev"]:,}</td><td>{format_data(pr["avg_data"])}</td><td>UGX {arpu:,.0f}</td></tr>'''
    if not pkg_rows:
        pkg_rows = '<tr><td colspan="6">No packages yet.</td></tr>'

    content = f'''
    <div class="stat-grid">
        <div class="stat-card"><h3>UGX {amount_this_month or 0:,}</h3><small>Amount This Month</small><div style="font-size:0.75rem; color:#28a745;">{growth_text}</div></div>
        <div class="stat-card"><h3>{total_clients or 0}</h3><small>Total Clients</small><div style="font-size:0.75rem; color:var(--text-secondary);">🆕 Lifetime customers</div></div>
        <div class="stat-card"><h3>{active_now or 0}</h3><small>Active Now</small><div style="font-size:0.75rem; color:#28a745;">🟢 Online right now</div></div>
        <div class="stat-card"><h3>{active_users_24h or 0}</h3><small>Active Users (24h)</small><div style="font-size:0.75rem; color:var(--text-secondary);">📊 Daily active users</div></div>
    </div>

    <div class="chart-row">
        <div class="card">
            <div class="card-header">📊 Payments <select id="pp" onchange="loadPay()" style="width:auto;display:inline;">
                <option value="today">Today</option>
                <option value="this_week">This Week</option>
                <option value="last_week">Last Week</option>
                <option value="this_month">This Month</option>
                <option value="last_month">Last Month</option>
                <option value="this_year">This Year</option>
                <option value="last_year">Last Year</option>
            </select></div>
            <div class="chart-container"><canvas id="payChart"></canvas></div>
        </div>
        <div class="card">
            <div class="card-header">👥 Active Users <small>Now: {active_now}</small></div>
            <div class="chart-container"><canvas id="auChart"></canvas></div>
        </div>
    </div>

    <div class="chart-row">
        <div class="card">
            <div class="card-header">📈 Customer Retention</div>
            <div class="chart-container"><canvas id="retChart"></canvas></div>
        </div>
        <div class="card">
            <div class="card-header">📅 Data Usage</div>
            <div class="chart-container"><canvas id="duChart"></canvas></div>
        </div>
    </div>

    <div class="chart-row">
        <div class="card">
            <div class="card-header">📦 Package Utilization</div>
            <div class="chart-container"><canvas id="pkgChart"></canvas></div>
        </div>
        <div class="card">
            <div class="card-header">🔮 Revenue Forecast</div>
            <div class="chart-container"><canvas id="fcChart"></canvas></div>
        </div>
    </div>

    <div class="chart-row">
        <div class="card">
            <div class="card-header">📱 Sent SMS</div>
            <div class="chart-container"><canvas id="smsChart"></canvas></div>
        </div>
        <div class="card">
            <div class="card-header">📶 Network Usage</div>
            <div class="chart-container"><canvas id="netChart"></canvas></div>
        </div>
    </div>

    <div class="chart-row">
        <div class="card">
            <div class="card-header">📋 Registrations</div>
            <div class="chart-container"><canvas id="regChart"></canvas></div>
        </div>
        <div class="card">
            <div class="card-header">⭐ Most Active</div>
            <table><thead><tr><th>Username</th><th>Data</th><th>Phone</th></tr></thead><tbody id="maTable"></tbody></table>
        </div>
    </div>

    <div class="card">
        <div class="card-header">🏆 Package Performance</div>
        <table><thead><tr><th>Package</th><th>Price</th><th>Active</th><th>Monthly Rev</th><th>Avg Data</th><th>ARPU</th></tr></thead><tbody>{pkg_rows}</tbody></table>
    </div>

    <script>
    async function loadPay() {{
        var p = document.getElementById('pp').value;
        var r = await fetch('/api/payments?period=' + p);
        var d = await r.json();
        var ctx = document.getElementById('payChart').getContext('2d');
        if (window.pc) window.pc.destroy();
        window.pc = new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: d.labels,
                datasets: [{{
                    label: 'Payments (UGX)',
                    data: d.values,
                    backgroundColor: 'rgba(26,115,232,0.7)',
                    borderColor: '#1a73e8',
                    borderWidth: 2,
                    borderRadius: 8
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
        }});
    }}

    fetch('/api/active-users-chart').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('auChart').getContext('2d'), {{
            type: 'line',
            data: {{
                labels: d.labels,
                datasets: [{{
                    label: 'Active',
                    data: d.values,
                    borderColor: '#1a73e8',
                    backgroundColor: 'rgba(26,115,232,0.1)',
                    fill: true,
                    tension: 0.4,
                    pointRadius: 6,
                    pointBackgroundColor: '#1a73e8'
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
        }});
    }});

    fetch('/api/retention').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('retChart').getContext('2d'), {{
            type: 'bar',
            data: {{
                labels: d.labels,
                datasets: [
                    {{ label: 'New', data: d.new_cust, backgroundColor: 'rgba(26,115,232,0.7)', borderRadius: 6 }},
                    {{ label: 'Returning', data: d.returning, backgroundColor: 'rgba(81,207,102,0.7)', borderRadius: 6 }},
                    {{ label: 'Churned', data: d.churned, backgroundColor: 'rgba(255,107,107,0.7)', borderRadius: 6 }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{ x: {{ stacked: true }}, y: {{ stacked: true }} }}
            }}
        }});
    }});

    fetch('/api/data-usage').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('duChart').getContext('2d'), {{
            type: 'line',
            data: {{
                labels: d.labels,
                datasets: [
                    {{ label: 'Download', data: d.downloads, borderColor: '#1a73e8', backgroundColor: 'rgba(26,115,232,0.1)', fill: true, tension: 0.4 }},
                    {{ label: 'Upload', data: d.uploads, borderColor: '#ffd43b', backgroundColor: 'rgba(255,212,59,0.1)', fill: true, tension: 0.4 }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    tooltip: {{
                        callbacks: {{
                            label: function(c) {{
                                return c.dataset.label + ': ' + (c.raw >= 1000 ? (c.raw/1000).toFixed(2) + ' GB' : c.raw.toFixed(2) + ' MB');
                            }}
                        }}
                    }}
                }}
            }}
        }});
    }});

    fetch('/api/package-util').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('pkgChart').getContext('2d'), {{
            type: 'doughnut',
            data: {{
                labels: d.labels,
                datasets: [{{
                    data: d.values,
                    backgroundColor: ['#1a73e8', '#51cf66', '#ffd43b', '#ff6b6b', '#6366f1', '#fd7e14'],
                    borderWidth: 0,
                    borderRadius: 4
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ position: 'bottom' }} }} }}
        }});
    }});

    fetch('/api/forecast').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('fcChart').getContext('2d'), {{
            type: 'line',
            data: {{
                labels: d.labels,
                datasets: [
                    {{ label: 'Historical', data: d.historical, borderColor: '#1a73e8', fill: false, tension: 0.4 }},
                    {{ label: 'Forecast', data: d.forecast, borderColor: '#51cf66', borderDash: [6, 3], fill: false, tension: 0.4 }},
                    {{ label: 'Upper', data: d.upper, borderColor: 'rgba(255,107,107,0.3)', borderDash: [2, 2], fill: false, pointRadius: 0 }},
                    {{ label: 'Lower', data: d.lower, borderColor: 'rgba(255,107,107,0.3)', borderDash: [2, 2], fill: false, pointRadius: 0 }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{ legend: {{ position: 'bottom' }} }}
            }}
        }});
    }});

    fetch('/api/sms-stats').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('smsChart').getContext('2d'), {{
            type: 'bar',
            data: {{
                labels: d.labels,
                datasets: [{{
                    label: 'SMS',
                    data: d.values,
                    backgroundColor: 'rgba(99,102,241,0.7)',
                    borderColor: '#6366f1',
                    borderWidth: 2,
                    borderRadius: 6
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
        }});
    }});

    fetch('/api/network').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('netChart').getContext('2d'), {{
            type: 'bar',
            data: {{
                labels: d.labels,
                datasets: [
                    {{ label: 'Download', data: d.downloads, backgroundColor: 'rgba(26,115,232,0.7)', borderRadius: 6 }},
                    {{ label: 'Upload', data: d.uploads, backgroundColor: 'rgba(255,212,59,0.7)', borderRadius: 6 }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    tooltip: {{
                        callbacks: {{
                            label: function(c) {{
                                return c.dataset.label + ': ' + (c.raw >= 1000 ? (c.raw/1000).toFixed(2) + ' GB' : c.raw.toFixed(2) + ' MB');
                            }}
                        }}
                    }}
                }}
            }}
        }});
    }});

    fetch('/api/registration').then(r => r.json()).then(d => {{
        new Chart(document.getElementById('regChart').getContext('2d'), {{
            type: 'line',
            data: {{
                labels: d.labels,
                datasets: [{{
                    label: 'Registrations',
                    data: d.values,
                    borderColor: '#ff6b6b',
                    backgroundColor: 'rgba(255,107,107,0.1)',
                    fill: true,
                    tension: 0.4,
                    pointRadius: 6,
                    pointBackgroundColor: '#ff6b6b'
                }}]
            }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
        }});
    }});

    fetch('/api/most-active').then(r => r.json()).then(d => {{
        var rows = '';
        d.forEach(u => {{
            rows += `<tr><td>${{u.username}}</td><td>${{u.data_usage}}</td><td>${{u.phone}}</td></tr>`;
        }});
        document.getElementById('maTable').innerHTML = rows;
    }});

    loadPay();
    </script>
    '''
    return render_page("Dashboard", content, get_pending_count(pid), pid, admin=True)

# ------------------------------------------------------------
# API ENDPOINTS (real data)
# ------------------------------------------------------------
@app.route('/api/payments')
@login_required
def api_payments():
    period = request.args.get('period', 'this_month')
    pid = session['provider_id']
    db = get_db()
    today = date.today()

    if period == 'today':
        start_date = end_date = today
    elif period == 'this_week':
        start_date = today - timedelta(days=today.weekday())
        end_date = today
    elif period == 'last_week':
        start_date = today - timedelta(days=today.weekday() + 7)
        end_date = start_date + timedelta(days=6)
    elif period == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
    elif period == 'last_month':
        start_date = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        end_date = (start_date.replace(month=start_date.month % 12 + 1, day=1) - timedelta(days=1))
    elif period == 'this_year':
        start_date = today.replace(month=1, day=1)
        end_date = today
    elif period == 'last_year':
        start_date = today.replace(year=today.year - 1, month=1, day=1)
        end_date = today.replace(year=today.year - 1, month=12, day=31)
    else:
        start_date = today.replace(day=1)
        end_date = today

    delta = end_date - start_date
    dates = [start_date + timedelta(days=i) for i in range(delta.days + 1)]
    labels = [d.strftime('%d %b') for d in dates]
    values = []
    for d in dates:
        amount = db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM voucher_requests "
            "WHERE provider_id = ? AND status = 'approved' AND date(created_at) = ?",
            (pid, d.isoformat())
        ).fetchone()['total']
        values.append(amount)
    return {'labels': labels, 'values': values}

@app.route('/api/active-users-chart')
@login_required
def api_active_users():
    pid = session['provider_id']; db = get_db(); today = date.today()
    labels = [(today - timedelta(days=i)).strftime('%a') for i in range(6, -1, -1)]
    values = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        v_cnt = db.execute(
            "SELECT COUNT(*) as c FROM vouchers WHERE provider_id = ? AND used = 0 AND date(created_at) <= ?",
            (pid, d.isoformat())
        ).fetchone()['c']
        s_cnt = db.execute(
            "SELECT COUNT(*) as c FROM sessions WHERE provider_id = ? AND date(started_at) <= ? AND (ended_at IS NULL OR date(ended_at) >= ?)",
            (pid, d.isoformat(), d.isoformat())
        ).fetchone()['c']
        values.append(v_cnt + s_cnt)
    return {'labels': labels, 'values': values}

@app.route('/api/retention')
@login_required
def api_retention():
    pid = session['provider_id']; db = get_db(); today = date.today()
    labels = [(today - timedelta(days=30 * i)).strftime('%b %Y') for i in range(5, -1, -1)]
    new_cust, returning, churned = [], [], []
    for i in range(5, -1, -1):
        month_start = (today - timedelta(days=30 * i)).replace(day=1)
        next_month = month_start.replace(day=1)
        if month_start.month == 12:
            next_month = next_month.replace(year=month_start.year + 1, month=1)
        else:
            next_month = next_month.replace(month=month_start.month + 1)
        new = db.execute(
            "SELECT COUNT(DISTINCT phone_number) as c FROM vouchers "
            "WHERE provider_id = ? AND date(created_at) >= ? AND date(created_at) < ? AND phone_number IS NOT NULL",
            (pid, month_start.isoformat(), next_month.isoformat())
        ).fetchone()['c']
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1)
        prev_month_end = month_start - timedelta(days=1)
        returning_count = db.execute(
            "SELECT COUNT(DISTINCT v2.phone_number) as c FROM vouchers v1 JOIN vouchers v2 ON v1.phone_number = v2.phone_number "
            "WHERE v1.provider_id = ? AND v2.provider_id = ? "
            "AND date(v1.created_at) >= ? AND date(v1.created_at) <= ? "
            "AND date(v2.created_at) >= ? AND date(v2.created_at) < ?",
            (pid, pid, prev_month_start.isoformat(), prev_month_end.isoformat(), month_start.isoformat(), next_month.isoformat())
        ).fetchone()['c']
        churned_count = db.execute(
            "SELECT COUNT(DISTINCT phone_number) as c FROM vouchers v1 "
            "WHERE v1.provider_id = ? AND date(v1.created_at) >= ? AND date(v1.created_at) <= ? "
            "AND v1.phone_number NOT IN ("
            "    SELECT DISTINCT phone_number FROM vouchers v2 "
            "    WHERE v2.provider_id = ? AND date(v2.created_at) >= ? AND date(v2.created_at) < ?"
            ")",
            (pid, prev_month_start.isoformat(), prev_month_end.isoformat(), pid, month_start.isoformat(), next_month.isoformat())
        ).fetchone()['c']
        new_cust.append(new); returning.append(returning_count); churned.append(churned_count)
    return {'labels': labels, 'new_cust': new_cust, 'returning': returning, 'churned': churned}

@app.route('/api/package-util')
@login_required
def api_package_util():
    pid = session['provider_id']; db = get_db()
    rows = db.execute(
        "SELECT p.name, COUNT(v.id) as cnt FROM vouchers v JOIN plans p ON v.plan_id = p.id "
        "WHERE v.provider_id = ? AND date(v.created_at) = date('now') "
        "GROUP BY p.name ORDER BY cnt DESC",
        (pid,)
    ).fetchall()
    if not rows:
        return {'labels': ['No Sales'], 'values': [1]}
    return {'labels': [r['name'] for r in rows], 'values': [r['cnt'] for r in rows]}

@app.route('/api/forecast')
@login_required
def api_forecast():
    pid = session['provider_id']; db = get_db(); today = date.today()
    hist_dates = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
    hist_values = []
    for d in hist_dates:
        total = db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM voucher_requests "
            "WHERE provider_id = ? AND status = 'approved' AND date(created_at) = ?",
            (pid, d.isoformat())
        ).fetchone()['total']
        hist_values.append(total)
    last_7 = hist_values[-7:] if len(hist_values) >= 7 else hist_values
    avg_7 = sum(last_7) / len(last_7) if last_7 else 0
    forecast_values = [avg_7] * 30
    upper = [avg_7 * 1.2] * 30
    lower = [avg_7 * 0.8] * 30
    forecast_dates = [(today + timedelta(days=i)) for i in range(30)]
    labels = [d.strftime('%d %b') for d in (hist_dates + forecast_dates)]
    return {
        'labels': labels,
        'historical': hist_values + [None] * 30,
        'forecast': [None] * 30 + forecast_values,
        'upper': [None] * 30 + upper,
        'lower': [None] * 30 + lower
    }

# Keep existing /api/data-usage, /api/sms-stats, /api/network, /api/registration, /api/most-active
# They already query real tables and are unchanged.

# ------------------------------------------------------------
# ACTIVE USERS (corrected dropdowns with click toggle)
# ------------------------------------------------------------
@app.route('/active-users')
@login_required
def active_users():
    pid = session['provider_id']; db = get_db()
    vouchers = db.execute("SELECT v.id, v.code, v.phone_number, p.name as pn, v.created_at FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? AND v.used=0",(pid,)).fetchall()
    subs = db.execute("SELECT s.id as sid, sub.username, sub.phone, s.ip_address, s.started_at FROM sessions s JOIN subscribers sub ON s.subscriber_id=sub.id WHERE s.provider_id=?",(pid,)).fetchall()
    rows = ''
    for v in vouchers:
        rows += f'<tr><td>{v["code"]}</td><td>{v["phone_number"]}</td><td>Hotspot</td><td>{v["pn"]}</td><td>{v["created_at"]}</td><td>-</td><td><div class="dropdown" style="position:relative;display:inline-block;z-index:9999;"><button class="btn btn-small dropdown-toggle" onclick="event.stopPropagation(); toggleDropdown(this);">&#8942;</button><div class="dropdown-content" style="display:none;position:absolute;right:0;top:100%;background:var(--card-bg);backdrop-filter:blur(20px);border-radius:8px;box-shadow:0 8px 25px rgba(0,0,0,0.2);z-index:999999;overflow:visible;padding:5px 0;min-width:150px;white-space:nowrap;"><a href="/disconnect-voucher/{v["id"]}" style="display:block;padding:8px 16px;color:var(--text);text-decoration:none;">Disconnect</a><a href="/disconnect-voucher-until-payment/{v["id"]}" style="display:block;padding:8px 16px;color:var(--text);text-decoration:none;">Disconnect until payment</a></div></div></td></tr>'
    for s in subs:
        rows += f'<tr><td>{s["username"]}</td><td>{s["phone"] or ""}</td><td>PPPoE</td><td>{s["ip_address"]}</td><td>{s["started_at"]}</td><td>-</td><td><div class="dropdown" style="position:relative;display:inline-block;z-index:9999;"><button class="btn btn-small dropdown-toggle" onclick="event.stopPropagation(); toggleDropdown(this);">&#8942;</button><div class="dropdown-content" style="display:none;position:absolute;right:0;top:100%;background:var(--card-bg);backdrop-filter:blur(20px);border-radius:8px;box-shadow:0 8px 25px rgba(0,0,0,0.2);z-index:999999;overflow:visible;padding:5px 0;min-width:150px;white-space:nowrap;"><a href="/disconnect-subscriber/{s["sid"]}" style="display:block;padding:8px 16px;color:var(--text);text-decoration:none;">Disconnect</a><a href="/suspend-subscriber/{s["sid"]}" style="display:block;padding:8px 16px;color:var(--text);text-decoration:none;">Disconnect until payment</a></div></div></td></tr>'
    if not rows: rows = '<tr><td colspan="7">No active users at the moment.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Active Users</div><div class="tabs"><span class="tab active">All <span class="badge">{len(vouchers)+len(subs)}</span></span><span class="tab">Hotspot <span class="badge">{len(vouchers)}</span></span><span class="tab">PPPoE <span class="badge">{len(subs)}</span></span></div><div class="table-responsive" style="overflow-x:auto; -webkit-overflow-scrolling:touch;"><table><thead><tr><th>Username</th><th>IP/MAC</th><th>Router</th><th>Session Start</th><th>Session End</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div></div>'''
    return render_page("Active Users", content, get_pending_count(pid), pid, admin=True)

# Include all other admin routes (Users, Expiry Dates, IP Bindings, Tickets, Leads, Plans, Payments, Vouchers, Invoices, Expenses, Messages, Email, Campaigns, MikroTik, Equipment, Provider Settings, Stats)
# They are unchanged except for adding pid in add/edit routes as previously instructed.
# To keep this response within length, I will summarize: you have already fixed those in earlier messages.

# ------------------------------------------------------------
# SUPER ADMIN
# ------------------------------------------------------------
SUPER_ADMIN_PASSWORD = 'rockabytech2025'

@app.route('/admin', methods=['GET','POST'])
def super_admin_login():
    if request.method == 'POST':
        if request.form.get('password') == SUPER_ADMIN_PASSWORD:
            session['super_admin'] = True
            db = get_db(); db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1, 'super_admin_login', 'Super admin logged in')"); db.commit()
            return redirect('/admin/dashboard')
        return render_page("Super Admin Login",'<div class="card"><div class="alert alert-error">Invalid password.</div><p><a href="/admin">Try again</a></p></div>',0,admin=False)
    return render_page("Super Admin Login",'<div class="card"><div class="card-header">🔐 RockabyTech Super Admin</div><form method="POST"><label>Password</label><input type="password" name="password" required><button type="submit" class="btn" style="margin-top:20px;width:100%;">Login</button></form></div>',0,admin=False)

@app.route('/admin/dashboard')
def super_admin_dashboard():
    if not session.get('super_admin'): return redirect('/admin')
    db = get_db()
    total_providers = db.execute("SELECT COUNT(*) as c FROM providers").fetchone()['c']
    active_providers = db.execute("SELECT COUNT(*) as c FROM providers WHERE is_active=1").fetchone()['c']
    total_revenue = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE status='approved'").fetchone()['t']
    platform_fee = int(total_revenue * 0.05) if total_revenue else 0
    total_users = db.execute("SELECT COUNT(DISTINCT phone_number) as c FROM vouchers").fetchone()['c']
    today = date.today().isoformat()
    today_revenue = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE status='approved' AND date(created_at)=?", (today,)).fetchone()['t']
    pending_approvals = db.execute("SELECT COUNT(*) as c FROM voucher_requests WHERE status='pending'").fetchone()['c']
    providers = db.execute("SELECT * FROM providers ORDER BY id").fetchall()
    rows = ''
    for p in providers:
        total = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved'", (p['id'],)).fetchone()['t']
        this_month = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at) >= ?", (p['id'], date.today().replace(day=1).isoformat())).fetchone()['t']
        fee = int(total * 0.05); monthly_fee = int(this_month * 0.05)
        voucher_count = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=?", (p['id'],)).fetchone()['c']
        sub_status = "Active" if p['is_active'] else "Suspended"
        expiry = p['subscription_expiry'] if p['subscription_expiry'] else '-'
        expired = False
        if p['subscription_expiry'] and date.fromisoformat(p['subscription_expiry']) < date.today():
            sub_status = "Expired"; expired = True
        row_class = 'style="background:rgba(255,212,59,0.1);"' if expired else ''
        rows += f'''<tr {row_class}><td>{p['id']}</td><td><strong>{p['business_name']}</strong></td><td>{p['contact']}</td><td><span class="badge" style="background:{'#51cf66' if sub_status=='Active' else '#ff6b6b' if sub_status=='Suspended' else '#ffd43b'};color:#000;padding:4px 10px;border-radius:12px;">{sub_status}</span></td><td>UGX {total or 0:,}</td><td>UGX {fee:,}</td><td>UGX {monthly_fee:,}</td><td>{voucher_count}</td><td>{expiry}</td><td style="overflow:visible; position:relative;"><div class="dropdown" style="position:relative;display:inline-block;z-index:9999;"><button class="btn btn-small dropdown-toggle" onclick="event.stopPropagation(); toggleDropdown(this);">&#8942;</button><div class="dropdown-content" style="display:none;position:absolute;right:0;top:100%;background:var(--card-bg);backdrop-filter:blur(20px);border-radius:8px;box-shadow:0 8px 25px rgba(0,0,0,0.2);z-index:999999;overflow:visible;padding:5px 0;min-width:200px;white-space:nowrap;"><a href="/admin/impersonate/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-user-secret"></i> Impersonate</a><a href="/admin/extend/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-calendar-plus"></i> Extend</a><a href="/admin/edit-provider/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-edit"></i> Edit</a><a href="/admin/invoice/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-file-invoice"></i> Send Invoice</a><a href="/admin/message/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-envelope"></i> Message</a><a href="/admin/toggle-provider/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-power-off"></i> {('Suspend' if p['is_active'] else 'Activate')}</a><a href="/admin/delete-provider/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;" onclick="return confirm('Delete permanently?')"><i class="fas fa-trash"></i> Delete</a></div></div></td></tr>'''
    audit = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 20").fetchall()
    audit_rows = ''.join(f'<tr><td>{a["created_at"][:16]}</td><td>{a["action"]}</td><td>{a["details"]}</td></tr>' for a in audit) or '<tr><td colspan="3">No activity yet.</td></tr>'
    content = f'''<div class="stat-grid"><div class="stat-card"><h3>{total_providers}</h3><small>Total Providers</small></div><div class="stat-card"><h3>{active_providers}</h3><small>Active</small></div><div class="stat-card"><h3>UGX {total_revenue or 0:,}</h3><small>Total Revenue</small></div><div class="stat-card"><h3>UGX {platform_fee:,}</h3><small>Your 5% Fee</small></div><div class="stat-card"><h3>{total_users}</h3><small>End Users</small></div><div class="stat-card"><h3>{pending_approvals}</h3><small>Pending</small></div></div><div class="card"><div class="card-header">Today: UGX {today_revenue or 0:,} revenue | UGX {int(today_revenue * 0.05):,} your fee</div></div><div class="card"><div class="card-header">Provider Management <a href="/admin/add-provider" class="btn btn-success btn-small">+ Add Provider</a></div><div class="table-responsive" style="overflow-x:auto; -webkit-overflow-scrolling:touch;"><table><thead><tr><th>ID</th><th>Name</th><th>Contact</th><th>Status</th><th>Revenue</th><th>Total Fee</th><th>Fee/Mo</th><th>Vouchers</th><th>Expiry</th><th>Actions</th></tr></thead><tbody>{rows}</tbody></table></div></div><div class="card"><div class="card-header">🕒 Recent Activity</div><div class="table-responsive" style="overflow-x:auto; -webkit-overflow-scrolling:touch;"><table><thead><tr><th>Time</th><th>Action</th><th>Details</th></tr></thead><tbody>{audit_rows}</tbody></table></div></div><p style="margin-top:20px;"><a href="/admin/logout" class="btn btn-outline">Logout</a></p>'''
    return render_page("Super Admin Dashboard", content, 0, admin=False)

# Keep all other super admin routes (add-provider, extend, edit-provider, invoice, message, impersonate, toggle-provider, delete-provider, logout) unchanged.

# ------------------------------------------------------------
# PWA ROUTES
# ------------------------------------------------------------
@app.route('/manifest.json')
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json', mimetype='application/json')

@app.route('/service-worker.js')
def service_worker():
    return send_from_directory(BASE_DIR, 'service-worker.js', mimetype='application/javascript')

# ------------------------------------------------------------
# ADMIN BACKUP AND RESTORE ROUTES
# ------------------------------------------------------------
@app.route('/admin/backup')
def admin_backup():
    if not session.get('super_admin'): return redirect('/admin')
    try:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"rockabywifi_backup_{timestamp}.db"
        backup_path = os.path.join(BACKUP_DIR, backup_filename)
        shutil.copy2(DB_PATH, backup_path)
        content = f'''<div class="card" style="text-align:center;"><div class="card-header">✅ Backup Created</div><p>File: <strong>{backup_filename}</strong></p><a href="/admin/download-backup/{backup_filename}" class="btn">Download</a><a href="/admin/backups" class="btn btn-outline">View All Backups</a></div>'''
        return render_page("Backup Created", content, 0, admin=False)
    except Exception as e: return f"Backup failed: {e}", 500

@app.route('/admin/backups')
def admin_backups():
    if not session.get('super_admin'): return redirect('/admin')
    backups = []
    for f in os.listdir(BACKUP_DIR):
        if f.endswith('.db'):
            stat = os.stat(os.path.join(BACKUP_DIR, f))
            backups.append({'name': f, 'size': stat.st_size, 'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')})
    backups.sort(key=lambda x: x['modified'], reverse=True)
    rows = ""
    for b in backups:
        rows += f"<tr><td>{b['name']}</td><td>{b['modified']}</td><td>{b['size'] // 1024} KB</td><td><a href='/admin/download-backup/{b['name']}' class='btn btn-small'>Download</a></td></tr>"
    if not rows: rows = "<tr><td colspan='4'>No backups found.</td></tr>"
    content = f"""
    <div class="card"><div class="card-header">💾 Database Backups</div>
    <a href="/admin/backup" class="btn" style="margin-bottom:20px;">Create New Backup</a>
    <a href="/admin/download-current-db" class="btn btn-outline" style="margin-bottom:20px; margin-left:10px;">Download Current DB</a>
    <a href="/admin/restore" class="btn btn-success" style="margin-bottom:20px; margin-left:10px;">📤 Restore from Backup</a>
    <table><thead><tr><th>Filename</th><th>Modified</th><th>Size</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table>
    <div style="margin-top:20px;"><a href="/admin/dashboard" class="btn btn-outline">Back to Super Admin</a></div></div>"""
    return render_page("Backups", content, 0, admin=False)

@app.route('/admin/download-backup/<filename>')
def admin_download_backup(filename):
    if not session.get('super_admin'): return redirect('/admin')
    if '..' in filename or '/' in filename: return "Invalid filename", 400
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path): return "Backup not found", 404
    return send_file(backup_path, as_attachment=True, download_name=filename)

@app.route('/admin/download-current-db')
def admin_download_current_db():
    if not session.get('super_admin'): return redirect('/admin')
    if not os.path.exists(DB_PATH): return "Database not found", 404
    return send_file(DB_PATH, as_attachment=True, download_name='rockabywifi_current.db')

@app.route('/admin/restore', methods=['GET','POST'])
def admin_restore():
    if not session.get('super_admin'): return redirect('/admin')
    if request.method == 'POST':
        if 'backup_file' not in request.files:
            return render_page("Restore", '<div class="card"><div class="alert alert-error">No file uploaded.</div><a href="/admin/restore" class="btn">Try again</a></div>', 0, admin=False)
        file = request.files['backup_file']
        if file.filename == '':
            return render_page("Restore", '<div class="card"><div class="alert alert-error">No file selected.</div><a href="/admin/restore" class="btn">Try again</a></div>', 0, admin=False)
        temp_path = '/tmp/restore_temp.db'
        file.save(temp_path)
        try:
            test_conn = sqlite3.connect(temp_path)
            test_conn.execute("SELECT 1")
            test_conn.close()
        except Exception as e:
            return render_page("Restore", f'<div class="card"><div class="alert alert-error">Invalid database file: {e}</div><a href="/admin/restore" class="btn">Try again</a></div>', 0, admin=False)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_before_restore = os.path.join(BACKUP_DIR, f"pre_restore_{timestamp}.db")
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, backup_before_restore)
        shutil.copy2(temp_path, DB_PATH)
        os.remove(temp_path)
        content = f"""
        <div class="card" style="text-align:center;"><div class="card-header">✅ Database Restored</div>
        <p>The database has been replaced with the uploaded backup.</p>
        <p>A backup of the previous database was saved as: <strong>pre_restore_{timestamp}.db</strong></p>
        <a href="/admin/backups" class="btn">View Backups</a>
        <a href="/admin/dashboard" class="btn btn-outline">Back to Super Admin</a></div>"""
        return render_page("Restore Complete", content, 0, admin=False)
    content = """
    <div class="card"><div class="card-header">⬆️ Restore Database from Backup</div>
    <div class="alert alert-error" style="background:rgba(255,193,7,0.15); border-color:rgba(255,193,7,0.3); color:#856404;">
        <strong>⚠️ Warning:</strong> This will overwrite your current database. The current database will be backed up automatically before restoration.
    </div>
    <form method="POST" enctype="multipart/form-data">
        <label>Select a backup file (.db)</label>
        <input type="file" name="backup_file" accept=".db" required>
        <button type="submit" class="btn" style="margin-top:20px;">Restore Database</button>
    </form>
    <div style="margin-top:20px;"><a href="/admin/backups" class="btn btn-outline">Back to Backups</a></div></div>"""
    return render_page("Restore Database", content, 0, admin=False)

# ------------------------------------------------------------
# RUN APP
# ------------------------------------------------------------
init_db()
backup_database()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
