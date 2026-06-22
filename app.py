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

def mt_get_active_users(pid):
    """
    Fetch active hotspot and PPPoE users from the MikroTik router(s)
    associated with the given provider_id.
    """
    # For now we use a single global router; you can later expand to per-provider routers
    api = mt_connect()
    if not api:
        return {'hotspot': [], 'ppp': []}
    
    hotspot = []
    ppp = []
    try:
        # Hotspot active users
        for u in api(cmd='/ip/hotspot/active/print'):
            hotspot.append({
                'username': u.get('user', ''),
                'ip': u.get('address', ''),
                'mac': u.get('mac-address', ''),
                'uptime': u.get('uptime', '')
            })
        # PPPoE active users
        for u in api(cmd='/ppp/active/print'):
            ppp.append({
                'username': u.get('name', ''),
                'ip': u.get('address', ''),
                'mac': u.get('caller-id', ''),
                'uptime': u.get('uptime', '')
            })
    except Exception as e:
        print(f"Error fetching active users: {e}")
    finally:
        api.close()
    
    return {'hotspot': hotspot, 'ppp': ppp}

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

    # ---- Providers table (already includes percent_fee and monthly_fee_ugx) ----
    c.execute('''CREATE TABLE IF NOT EXISTS providers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_name TEXT NOT NULL,
        contact TEXT,
        password_hash TEXT NOT NULL,
        subscription_expiry DATE,
        percent_fee REAL DEFAULT 5.0,
        monthly_fee_ugx INTEGER DEFAULT 20000,
        auto_approve INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1,
        mtn_number TEXT,
        airtel_number TEXT,
        poster_image TEXT,
        logo_image TEXT,
        support_phone TEXT,
        yo_username TEXT,
        yo_password TEXT,
        yo_auto_pay INTEGER DEFAULT 0
    )''')

    # ---- Add missing columns to providers (including billing columns) ----
    c.execute("PRAGMA table_info(providers)")
    existing = [col[1] for col in c.fetchall()]
    for col in ['poster_image','logo_image','support_phone','yo_username','yo_password','yo_auto_pay','percent_fee','monthly_fee_ugx']:
        if col not in existing:
            if col == 'percent_fee':
                c.execute("ALTER TABLE providers ADD COLUMN percent_fee REAL DEFAULT 5.0")
            elif col == 'monthly_fee_ugx':
                c.execute("ALTER TABLE providers ADD COLUMN monthly_fee_ugx INTEGER DEFAULT 20000")
            else:
                c.execute(f"ALTER TABLE providers ADD COLUMN {col} TEXT")

    # ---- Plans ----
    c.execute('''CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        price_ugx INTEGER NOT NULL,
        is_active INTEGER DEFAULT 1,
        is_public INTEGER DEFAULT 1,
        speed_down TEXT,
        speed_up TEXT,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')
    c.execute("PRAGMA table_info(plans)")
    plan_cols = [col[1] for col in c.fetchall()]
    for col in ['is_public','speed_down','speed_up']:
        if col not in plan_cols:
            c.execute(f"ALTER TABLE plans ADD COLUMN {col} TEXT")

    # ---- Voucher requests ----
    c.execute('''CREATE TABLE IF NOT EXISTS voucher_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        phone_number TEXT NOT NULL,
        plan_id INTEGER,
        raw_sms TEXT NOT NULL,
        transaction_id TEXT,
        amount INTEGER,
        recipient TEXT,
        payment_date TEXT,
        status TEXT DEFAULT 'pending',
        voucher_code TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- Vouchers ----
    c.execute('''CREATE TABLE IF NOT EXISTS vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        code TEXT UNIQUE NOT NULL,
        plan_id INTEGER,
        payment_method TEXT DEFAULT 'sms',
        phone_number TEXT,
        used INTEGER DEFAULT 0,
        used_at TIMESTAMP,
        mac_address TEXT,
        ip_address TEXT,
        batch_id TEXT,
        expiry_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')
    c.execute("PRAGMA table_info(vouchers)")
    vouch_cols = [col[1] for col in c.fetchall()]
    for col in ['batch_id','expiry_date']:
        if col not in vouch_cols:
            c.execute(f"ALTER TABLE vouchers ADD COLUMN {col} TEXT")

    # ---- Subscribers ----
    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        phone TEXT,
        current_ip TEXT,
        suspended INTEGER DEFAULT 0,
        package_name TEXT,
        expiry_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- Sessions ----
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher_id INTEGER,
        subscriber_id INTEGER,
        provider_id INTEGER,
        mac_address TEXT,
        ip_address TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP,
        data_download REAL DEFAULT 0,
        data_upload REAL DEFAULT 0,
        FOREIGN KEY(voucher_id) REFERENCES vouchers(id),
        FOREIGN KEY(subscriber_id) REFERENCES subscribers(id)
    )''')

    # ---- Restricted ----
    c.execute('''CREATE TABLE IF NOT EXISTS restricted (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER,
        phone_number TEXT,
        mac_address TEXT,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Settings ----
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- Data sessions ----
    c.execute('''CREATE TABLE IF NOT EXISTS data_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        phone_number TEXT,
        session_date DATE,
        data_download REAL DEFAULT 0,
        data_upload REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- SMS log ----
    c.execute('''CREATE TABLE IF NOT EXISTS sms_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        phone_number TEXT,
        message TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- User activity ----
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        phone_number TEXT,
        action TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- Tickets ----
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'open',
        priority TEXT DEFAULT 'medium',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- Leads ----
    c.execute('''CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        source TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- Expenses ----
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        category TEXT,
        expense_date DATE,
        payment_method TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # ---- Invoices ----
    c.execute('''CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        invoice_no TEXT UNIQUE,
        user_id INTEGER,
        amount REAL,
        paid_amount REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        due_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Notifications ----
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Campaigns ----
    c.execute('''CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        kind TEXT,
        type TEXT,
        start_date DATE,
        end_date DATE,
        status TEXT DEFAULT 'inactive',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Equipment ----
    c.execute('''CREATE TABLE IF NOT EXISTS equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        model TEXT,
        serial_number TEXT,
        user_id INTEGER,
        price REAL,
        paid_amount REAL DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- MikroTik routers ----
    c.execute('''CREATE TABLE IF NOT EXISTS mikrotik_routers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        ip_address TEXT,
        username TEXT,
        password TEXT,
        api_port INTEGER DEFAULT 8728,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Trial used ----
    c.execute('''CREATE TABLE IF NOT EXISTS trial_used (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip_address TEXT UNIQUE,
        used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Yo! transactions ----
    c.execute('''CREATE TABLE IF NOT EXISTS yo_tx (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        tx_ref TEXT UNIQUE,
        phone TEXT,
        amount INTEGER,
        status TEXT DEFAULT 'pending',
        voucher_code TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Expiry dates ----
    c.execute('''CREATE TABLE IF NOT EXISTS expiry_dates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        expiry_date TIMESTAMP,
        grace_period INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id),
        FOREIGN KEY(user_id) REFERENCES subscribers(id)
    )''')

    # ---- IP bindings ----
    c.execute('''CREATE TABLE IF NOT EXISTS ip_bindings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        mikrotik_id INTEGER,
        name TEXT NOT NULL,
        package_id INTEGER,
        dhcp_lease TEXT,
        address TEXT NOT NULL,
        mac_address TEXT NOT NULL,
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id),
        FOREIGN KEY(mikrotik_id) REFERENCES mikrotik_routers(id),
        FOREIGN KEY(package_id) REFERENCES plans(id)
    )''')

    # ---- Audit log ----
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        action TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ---- Default provider (if not exists) ----
    c.execute("SELECT COUNT(*) FROM providers WHERE id=1")
    if c.fetchone()[0] == 0:
        hashed = generate_password_hash('admin123')
        c.execute("""INSERT INTO providers
            (id, business_name, contact, password_hash, subscription_expiry, is_active,
             mtn_number, airtel_number, support_phone, percent_fee, monthly_fee_ugx)
            VALUES (1,?,?,?,?,?,?,?,?,?,?)""",
                  ('RockabyWiFi','256751318876', hashed, date.today()+timedelta(days=3650),
                   1, '0785686404', '0751318876', '256751318876', 5.0, 20000))
        for name, mins, price in [('3 Hours',180,500),('24 Hours',1440,1000),('Weekly',10080,5000),('Monthly',43200,20000)]:
            c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public, speed_down, speed_up) VALUES (1,?,?,?,1,'5M','2M')", (name,mins,price))
        c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public, speed_down, speed_up) VALUES (1,'Free Trial',5,0,0,'1M','512k')")
        c.execute("INSERT INTO settings (provider_id, key, value) VALUES (1,'auto_approve','1')")
    else:
        # Ensure Free Trial plan exists
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


def get_setting(provider_id, key, default=None):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE provider_id=? AND key=?", (provider_id, key)).fetchone()
    return row['value'] if row else default

def set_setting(provider_id, key, value):
    db = get_db()
    # Check if the key already exists for this provider
    existing = db.execute(
        "SELECT id FROM settings WHERE provider_id = ? AND key = ?",
        (provider_id, key)
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE settings SET value = ? WHERE provider_id = ? AND key = ?",
            (value, provider_id, key)
        )
    else:
        db.execute(
            "INSERT INTO settings (provider_id, key, value) VALUES (?, ?, ?)",
            (provider_id, key, value)
        )
    db.commit()

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
            background: linear-gradient(135deg, rgba(26,115,232,0.2), rgba(255,107,107,0.1));
        }
        .sidebar-header img { height:40px; width:40px; border-radius:10px; box-shadow:0 4px 12px rgba(0,0,0,0.3); }
        .sidebar-header h3 { font-size:1.2rem; font-weight:700; letter-spacing:0.5px; }
        .sidebar-menu { padding:10px 0; }
        .sidebar-menu a {
            display:flex; align-items:center; gap:10px; padding:12px 24px; color:var(--text-secondary);
            text-decoration:none; transition:all 0.2s; font-size:0.9rem; border-left:3px solid transparent;
        }
        .sidebar-menu a:hover, .sidebar-menu a.active {
            background:rgba(26,115,232,0.08); color:var(--primary); border-left-color: var(--primary);
        }
        .sidebar-menu a:hover, .sidebar-menu a.active {
    background: linear-gradient(90deg, rgba(245,175,25,0.2), transparent);
    color: #f5af19;
    border-left-color: #f5af19;
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
        .stat-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:18px; margin-bottom:24px; }
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
        
        /* ========== DROPDOWN FIX (Hover-based) ========== */
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

        /* ===== MOBILE RESPONSIVENESS IMPROVEMENTS ===== */
        @media (max-width: 768px) {
            .sidebar {
                width: 280px;
                transform: translateX(-100%);
            }
            .sidebar.open {
                transform: translateX(0);
            }
            .main-content {
                margin-left: 0 !important;
            }
            .card {
                padding: 16px !important;
                margin-bottom: 12px;
            }
            .card-header {
                font-size: 1rem;
                flex-wrap: wrap;
                gap: 10px;
            }
            .stat-grid {
                grid-template-columns: repeat(2, 1fr) !important;
                gap: 10px !important;
            }
            .stat-card {
                padding: 12px !important;
            }
            .stat-card h3 {
                font-size: 1.2rem !important;
            }
            .stat-card small {
                font-size: 0.7rem;
            }
            .chart-row {
                flex-direction: column;
            }
            .chart-row .card {
                min-width: 100% !important;
            }
            .table-responsive {
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                margin: 0 -8px;
                padding: 0 8px;
            }
            table {
                font-size: 0.8rem;
            }
            th, td {
                padding: 6px 8px !important;
                white-space: nowrap;
            }
            .tabs {
                gap: 5px;
                overflow-x: auto;
                flex-wrap: nowrap;
                -webkit-overflow-scrolling: touch;
                padding-bottom: 5px;
            }
            .tab {
                padding: 6px 12px;
                font-size: 0.8rem;
                white-space: nowrap;
            }
            input, textarea, select {
                font-size: 0.9rem;
                padding: 8px 10px;
            }
            .btn {
                padding: 8px 16px;
                font-size: 0.85rem;
            }
            .container {
                padding: 0 12px;
                margin: 12px auto;
            }
            #invoiceModal > div {
                margin: 10px;
                padding: 20px !important;
            }
            #invoiceModal table {
                font-size: 0.8rem;
            }
            #invoiceModal th, #invoiceModal td {
                padding: 6px 8px !important;
            }
            .card-header input[type="text"] {
                width: 120px !important;
                font-size: 0.8rem;
            }
            .topbar {
                padding: 10px 12px !important;
                flex-wrap: wrap;
            }
            .topbar-right {
                gap: 10px;
            }
            .topbar-right span {
                font-size: 0.85rem !important;
            }
            .hamburger {
                font-size: 1.2rem;
            }
        }
        {theme_style}   <!-- THEME CSS INJECTED INSIDE THE STYLE BLOCK -->
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

        // ===== MOBILE DROPDOWN TOGGLE (Touch-friendly 3‑dot menus) =====
        document.addEventListener('DOMContentLoaded', function() {
            if ('ontouchstart' in window) {
                document.querySelectorAll('.dropdown').forEach(function(dropdown) {
                    dropdown.addEventListener('click', function(e) {
                        e.preventDefault();
                        var content = this.querySelector('.dropdown-content');
                        if (content) {
                            // Close all other dropdowns
                            document.querySelectorAll('.dropdown-content').forEach(function(other) {
                                if (other !== content) other.style.display = 'none';
                            });
                            content.style.display = content.style.display === 'block' ? 'none' : 'block';
                        }
                    });
                });
                // Close dropdowns when clicking outside
                document.addEventListener('click', function(e) {
                    if (!e.target.closest('.dropdown')) {
                        document.querySelectorAll('.dropdown-content').forEach(function(el) {
                            el.style.display = 'none';
                        });
                    }
                });
            }
        });
    </script>
</body>
</html>
"""

# ------------------------------------------------------------
# RENDER_PAGE FUNCTION
# ------------------------------------------------------------
def render_page(title, content, pending_count=0, provider_id=1, admin=False, theme_style=''):
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

        # ========== NEW EXPANDED TOPBAR ==========
        topbar = f'''
<div class="topbar">
    <button class="hamburger" onclick="toggleSidebar()">&#9776;</button>
    <div class="topbar-right">
        <button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark/light mode">🌓</button>
        <span style="color:#1a73e8; font-weight:600;">Welcome, {session["provider_name"]}</span>
        <div class="settings-dropdown">
            <a href="#" style="color:var(--text);text-decoration:none;font-size:1.3rem;"><i class="fas fa-cog"></i></a>
            <div class="settings-dropdown-content">
                <a href="/settings"><i class="fas fa-sliders-h"></i> Settings</a>
                <a href="/billing"><i class="fas fa-file-invoice"></i> Billing & Subscription</a>
                <a href="/system-users"><i class="fas fa-users-cog"></i> System Users</a>
                <a href="/system-logs"><i class="fas fa-history"></i> System Logs</a>
                <a href="/refer"><i class="fas fa-share-alt"></i> Refer a Friend</a>
                <a href="/docs"><i class="fas fa-book"></i> Documentation</a>
                <hr style="border-color:var(--border); margin:5px 0;">
                <a href="/logout"><i class="fas fa-sign-out-alt"></i> Logout</a>
            </div>
        </div>
    </div>
</div>
'''
        layout = 'admin-layout'
    else:
        sidebar = ''
        # Public topbar (no admin)
        topbar = '''
<div class="topbar" style="background:transparent;box-shadow:none;">
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

    return base_template.replace('{title}', title) \
                    .replace('{layout_class}', layout) \
                    .replace('{sidebar_html}', sidebar) \
                    .replace('{topbar_html}', topbar) \
                    .replace('{content}', content) \
                    .replace('{support_phone}', sp) \
                    .replace('{theme_style}', theme_style)

# ------------------------------------------------------------
# CUSTOMER ROUTES
# ------------------------------------------------------------
@app.route('/')
def home():
    pid = request.args.get('pid', 1, type=int)
    p = get_provider(pid)
    if not p:
        return "Provider not found.", 404

    # Get the selected theme
    theme = get_setting(pid, 'captive_portal_theme', 'default')

    # Theme styles
    theme_styles = {
        'default': '',
        'neon': '''
        :root {
            --primary: #00d4ff;
            --primary-dark: #0099cc;
            --bg: #0a0a1a;
            --card-bg: rgba(20, 20, 40, 0.85);
            --text: #e0e0ff;
            --text-secondary: #a0a0cc;
            --border: rgba(0, 212, 255, 0.3);
            --shadow: 0 8px 32px rgba(0, 212, 255, 0.2);
        }
        body { background: #0a0a1a; background-image: radial-gradient(circle at 20% 30%, rgba(0, 212, 255, 0.1) 0%, transparent 50%), radial-gradient(circle at 80% 70%, rgba(255, 0, 150, 0.1) 0%, transparent 50%); }
        .hero { background: rgba(0, 212, 255, 0.05) !important; border: 1px solid rgba(0, 212, 255, 0.2) !important; }
        .hero h1 { background: linear-gradient(135deg, #00d4ff, #ff00a0); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
        .btn { background: linear-gradient(135deg, #00d4ff, #0099cc); box-shadow: 0 0 20px rgba(0, 212, 255, 0.4); }
        .card { background: rgba(20, 20, 40, 0.85); border: 1px solid rgba(0, 212, 255, 0.2); }
        .stat-card { background: rgba(20, 20, 40, 0.7); border: 1px solid rgba(0, 212, 255, 0.15); }
        .voucher-code { background: linear-gradient(135deg, #00d4ff, #ff00a0); }
        ''',
        'minimalist': '''
        :root {
            --primary: #2c3e50;
            --primary-dark: #1a252f;
            --bg: #f8f9fa;
            --card-bg: rgba(255,255,255,0.95);
            --text: #2c3e50;
            --text-secondary: #7f8c8d;
            --border: #ecf0f1;
            --shadow: 0 2px 10px rgba(0,0,0,0.05);
        }
        body { background: #f8f9fa; }
        .hero { background: #ffffff !important; border: 1px solid #ecf0f1 !important; }
        .hero h1 { background: linear-gradient(135deg, #2c3e50, #3498db); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
        .btn { background: #2c3e50; box-shadow: none; }
        .card { background: #ffffff; box-shadow: 0 2px 10px rgba(0,0,0,0.05); border: 1px solid #ecf0f1; }
        .stat-card { background: #ffffff; border: 1px solid #ecf0f1; }
        .voucher-code { background: #2c3e50; }
        '''
    }
    theme_css = theme_styles.get(theme, '')

    # Build the page
    bn = p['business_name'] if p else 'RockabyWiFi'
    logo = f'<img src="/static/uploads/{p["logo_image"]}" class="provider-logo" alt="{bn}">' if p and p['logo_image'] else ''
    poster = f'<img src="/static/uploads/{p["poster_image"]}" class="provider-poster" alt="Poster">' if p and p['poster_image'] else ''
    hero_logo = '<img src="/static/ug-06.png" alt="RockabyWiFi" style="height:80px; width:80px; border-radius:16px; object-fit:cover; margin-bottom:15px; box-shadow:0 4px 15px rgba(0,0,0,0.2);">'

    db = get_db()
    plans = db.execute(
        "SELECT id, name, duration_minutes, price_ugx, speed_down, speed_up "
        "FROM plans WHERE provider_id = ? AND is_active = 1 AND is_public = 1 "
        "ORDER BY price_ugx ASC",
        (pid,)
    ).fetchall()

    package_cards = ''
    if plans:
        for plan in plans:
            speed = f"{plan['speed_down']}/{plan['speed_up']}" if plan['speed_down'] and plan['speed_up'] else 'Standard'
            package_cards += f'''
            <div class="card package-card" style="text-align:center; display:flex; flex-direction:column; justify-content:space-between;">
                <div>
                    <h3 style="margin-bottom:10px;">{plan['name']}</h3>
                    <p style="font-size:0.9rem; color:var(--text-secondary);">⏱ {plan['duration_minutes']} minutes</p>
                    <p style="font-size:1.1rem; font-weight:600; color:var(--primary);">UGX {plan['price_ugx']:,}</p>
                    <p style="font-size:0.8rem; color:var(--text-secondary);">Speed: {speed}</p>
                </div>
                <a href="/sms-verify?pid={pid}&plan_id={plan['id']}" class="btn" style="margin-top:15px; width:100%;">Buy Now</a>
            </div>
            '''
    else:
        package_cards = '<p style="grid-column:1/-1; text-align:center;">No packages available at the moment.</p>'

    content = f'''
    <div class="card" style="display:flex; align-items:center; gap:15px; flex-wrap:wrap;">{logo}<h2 style="margin:0;">{bn}</h2></div>
    {poster}
    <div class="hero" style="background: linear-gradient(135deg, rgba(26,115,232,0.15), rgba(99,102,241,0.1)); border-radius:var(--radius); padding:40px; text-align:center; margin-bottom:30px; border:1px solid var(--glass-border);">
        {hero_logo}
        <h1 style="font-size:2.5rem; margin-bottom:15px; background:linear-gradient(135deg, var(--primary), #6366f1); -webkit-background-clip:text; -webkit-text-fill-color:transparent;">Fast & Reliable WiFi</h1>
        <p style="font-size:1.1rem; color:var(--text-secondary); margin-bottom:20px;">Choose a plan below and get connected in minutes.</p>
    </div>
    <div class="card-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:20px; margin-bottom:30px;">
        <div class="card package-card" style="text-align:center; display:flex; flex-direction:column; justify-content:space-between;">
            <div>
                <h3 style="margin-bottom:10px;">🔐 Login</h3>
                <p style="font-size:0.9rem; color:var(--text-secondary);">Already have an account?<br>Login to manage your sessions.</p>
            </div>
            <a href="/subscriber-login?pid={pid}" class="btn" style="margin-top:15px; width:100%;">Login</a>
        </div>
        <div class="card package-card" style="text-align:center; display:flex; flex-direction:column; justify-content:space-between;">
            <div>
                <h3 style="margin-bottom:10px;">🎟️ Redeem Voucher</h3>
                <p style="font-size:0.9rem; color:var(--text-secondary);">Have a voucher code?<br>Redeem it here to get connected.</p>
            </div>
            <a href="/redeem?pid={pid}" class="btn" style="margin-top:15px; width:100%;">Redeem</a>
        </div>
        {package_cards}
    </div>
    <p style="text-align:center; margin-top:10px;">
        <a href="/free-trial?pid={pid}" class="btn btn-outline" style="background: linear-gradient(135deg, #28a745, #51cf66); color:white; border:none; padding:12px 30px;">🎁 Try 5 Minutes Free</a>
    </p>
    '''
    return render_page("Get Internet Access", content, get_pending_count(pid), pid, admin=False, theme_style=theme_css)


@app.route('/free-trial')
def free_trial():
    pid = request.args.get('pid', 1, type=int)
    ip = request.remote_addr
    db = get_db()
    if db.execute("SELECT COUNT(*) as cnt FROM trial_used WHERE ip_address=? AND provider_id=?", (ip, pid)).fetchone()['cnt'] > 0:
        return render_page("Free Trial", '<div class="card"><div class="alert alert-error">You have already used your free trial.</div><p><a href="/?pid=' + str(pid) + '" class="btn">Back to Home</a></p></div>', get_pending_count(pid), pid, admin=False)
    trial = db.execute("SELECT id, duration_minutes FROM plans WHERE provider_id=? AND name='Free Trial' AND is_active=1", (pid,)).fetchone()
    if not trial:
        return render_page("Free Trial", '<div class="card"><div class="alert alert-error">Trial not available.</div></div>', get_pending_count(pid), pid, admin=False)
    code = generate_voucher_code()
    db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, ip_address, used) VALUES (?, ?, ?, 'trial', ?, 0)", (pid, code, trial['id'], ip))
    db.execute("INSERT INTO trial_used (ip_address) VALUES (?)", (ip,))
    db.commit()
    content = f'''<div class="card"><div class="alert alert-success">Free trial activated!</div><p><strong>Your Voucher Code:</strong></p><div class="voucher-code" id="vc">{code}</div><button class="copy-btn" onclick="navigator.clipboard.writeText('{code}')">📋 Copy</button><p style="margin-top:10px;">Use this code on the <a href="/redeem?pid={pid}">Redeem page</a> to connect for 5 minutes.</p><a href="/?pid={pid}" class="btn">Back to Home</a></div>'''
    return render_page("Free Trial", content, get_pending_count(pid), pid, admin=False)


@app.route('/redeem', methods=['GET','POST'])
def redeem():
    pid = request.args.get('pid', 1, type=int)
    if request.method == 'POST':
        code = request.form['code'].strip().upper()
        db = get_db()
        v = db.execute("SELECT v.id, v.phone_number, p.duration_minutes FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.code=? AND v.used=0 AND v.provider_id=?", (code, pid)).fetchone()
        if v:
            db.execute("UPDATE vouchers SET used=1, used_at=CURRENT_TIMESTAMP WHERE id=?", (v['id'],))
            db.commit()
            mt_add_user(v['phone_number'] or 'trial', v['duration_minutes'])
            return render_page("Voucher Redeemed", '<div class="card"><div class="alert alert-success">Connected! Enjoy your internet access.</div><a href="/?pid=' + str(pid) + '" class="btn">Back to Home</a></div>', get_pending_count(pid), pid, admin=False)
        return render_page("Redeem Voucher", '<div class="card"><div class="alert alert-error">Invalid or already used voucher code.</div><form method="POST"><input type="hidden" name="pid" value="' + str(pid) + '"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px;width:100%;">Redeem</button></form></div>', get_pending_count(pid), pid, admin=False)
    return render_page("Redeem Voucher", f'<div class="card"><div class="card-header">Redeem Voucher</div><form method="POST"><input type="hidden" name="pid" value="{pid}"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px;width:100%;">Redeem</button></form></div>', get_pending_count(pid), pid, admin=False)


@app.route('/sms-verify', methods=['GET','POST'])
def sms_verify():
    pid = request.args.get('pid', 1, type=int)
    phone = request.args.get('phone', '')
    plan_id = request.args.get('plan_id', '1')
    pc = get_pending_count(pid)
    db = get_db()
    plan = db.execute("SELECT * FROM plans WHERE id=? AND provider_id=?", (plan_id, pid)).fetchone()
    if not plan:
        return "Invalid plan selected.", 400

    provider = get_provider(pid)
    active_method = get_setting(pid, 'active_payment_method', 'manual')
    display_name = get_setting(pid, 'payment_name', provider['business_name'] if provider else 'RockabyWiFi')

    # ---- POST: only for manual SMS verification ----
    if request.method == 'POST':
        if active_method != 'manual':
            return render_page(
                "Payment Error",
                '<div class="card"><div class="alert alert-error">This payment method does not support manual SMS verification.</div></div>',
                pc, pid, admin=False
            )

        phone = request.form['phone'].strip()
        plan_id = int(request.form['plan_id'])
        raw = request.form['raw_sms'].strip()
        parsed = parse_airtel_sms(raw) if 'TID' in raw or 'SENT.TID' in raw else parse_mtn_sms(raw)

        err = None
        if not parsed['tid']:
            err = "Could not detect Transaction ID."
        elif not parsed['amount']:
            err = "Could not detect amount."
        elif parsed['amount'] != plan['price_ugx']:
            err = f"Amount mismatch. Expected UGX {plan['price_ugx']:,}."
        elif not parsed.get('recipient_name'):
            err = "Could not detect recipient."
        else:
            mtn = clean_number(provider['mtn_number']) if provider and provider['mtn_number'] else ''
            air = clean_number(provider['airtel_number']) if provider and provider['airtel_number'] else ''
            sms_num = clean_number(parsed.get('recipient_number', '')) if parsed.get('recipient_number') else ''
            if sms_num:
                if sms_num != mtn and sms_num != air:
                    err = "Payment not sent to the correct provider number."
            else:
                rl = parsed['recipient_name'].lower()
                if provider['mtn_number'] and provider['mtn_number'] not in rl and provider['airtel_number'] and provider['airtel_number'] not in rl:
                    err = "Payment not sent to the correct provider number."

        if err:
            content = f'''
            <div class="card">
                <div class="alert alert-error">{err}</div>
                <form method="POST">
                    <input type="hidden" name="phone" value="{phone}">
                    <input type="hidden" name="plan_id" value="{plan_id}">
                    <input type="hidden" name="pid" value="{pid}">
                    <label>Paste Full MTN/Airtel SMS Here</label>
                    <textarea name="raw_sms" rows="6" required></textarea>
                    <button type="submit" class="btn" style="margin-top:20px;width:100%;">Verify Payment</button>
                </form>
            </div>
            '''
            return render_page("Verify Payment", content, pc, pid, admin=False)

        # Check duplicate
        if db.execute(
            "SELECT COUNT(*) as cnt FROM voucher_requests WHERE transaction_id=? AND provider_id=?",
            (parsed['tid'], pid)
        ).fetchone()['cnt'] > 0:
            content = f'''
            <div class="card">
                <div class="alert alert-error">This Transaction ID has already been used.</div>
                <p><a href="/?pid={pid}" class="btn">Back to Home</a></p>
            </div>
            '''
            return render_page("Verify Payment", content, pc, pid, admin=False)

        auto = provider['auto_approve'] if provider else 1
        status = 'approved' if auto else 'pending'
        vc = None
        rf = f"{parsed.get('recipient_name','')} {parsed.get('recipient_number','')}".strip()

        if status == 'approved':
            vc = generate_voucher_code()
            db.execute(
                "INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number, used, used_at) "
                "VALUES (?,?,?,'sms',?,1,CURRENT_TIMESTAMP)",
                (pid, vc, plan_id, phone)
            )
            db.execute(
                "INSERT INTO voucher_requests "
                "(provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status, voucher_code) "
                "VALUES (?,?,?,?,?,?,?,?,'approved',?)",
                (pid, phone, plan_id, raw, parsed['tid'], parsed['amount'], rf, parsed['date'], vc)
            )
            db.commit()
            # Activate internet immediately
            mt_add_user(phone, plan['duration_minutes'])
            return redirect("https://www.google.com")

        else:
            db.execute(
                "INSERT INTO voucher_requests "
                "(provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status) "
                "VALUES (?,?,?,?,?,?,?,?,'pending')",
                (pid, phone, plan_id, raw, parsed['tid'], parsed['amount'], rf, parsed['date'])
            )
            db.commit()
            content = f'''
            <div class="card">
                <div class="alert alert-success">Payment submitted! Waiting for approval.</div>
                <p><a href="/?pid={pid}" class="btn">Back to Home</a></p>
            </div>
            '''
            return render_page("Verification Result", content, get_pending_count(pid), pid, admin=False)

    # ---- GET: handle based on active payment method ----
    if active_method == 'manual':
        content = f'''
        <div class="card">
            <div class="card-header">Pay for Internet</div>
            <p><strong>Selected Plan:</strong> {plan["name"]} – {plan["duration_minutes"]} min – UGX {plan["price_ugx"]:,}</p>
            <p><strong>Pay to:</strong></p>
            <p>MTN: {provider["mtn_number"] if provider and provider["mtn_number"] else 'N/A'} | Airtel: {provider["airtel_number"] if provider and provider["airtel_number"] else 'N/A'}</p>
            <p style="color:#666;">Name: {display_name}</p>
            <hr>
            <p style="margin-top:15px;"><strong>After payment, paste the full SMS below:</strong></p>
            <form method="POST">
                <input type="hidden" name="phone" value="{phone}">
                <input type="hidden" name="plan_id" value="{plan_id}">
                <input type="hidden" name="pid" value="{pid}">
                <label>Paste Full MTN/Airtel SMS Here</label>
                <textarea name="raw_sms" rows="6" required></textarea>
                <button type="submit" class="btn" style="margin-top:20px;width:100%;">Verify Payment</button>
            </form>
        </div>
        '''
        return render_page("Verify Payment", content, pc, pid, admin=False)

    elif active_method == 'yo':
        if not provider['yo_username'] or not provider['yo_password']:
            return render_page(
                "Payment Error",
                '<div class="card"><div class="alert alert-error">Yo! Payments is not configured. Please contact the provider.</div></div>',
                pc, pid, admin=False
            )
        if phone:
            return redirect(url_for('yo_pay', pid=pid, phone=phone, plan_id=plan_id))
        else:
            content = f'''
            <div class="card">
                <div class="card-header">Pay with Yo! Payments</div>
                <p>You are about to purchase <strong>{plan["name"]}</strong> for UGX {plan["price_ugx"]:,}.</p>
                <form method="GET" action="/yo-pay">
                    <input type="hidden" name="pid" value="{pid}">
                    <input type="hidden" name="plan_id" value="{plan_id}">
                    <label>Your Phone Number *</label>
                    <input type="tel" name="phone" required>
                    <button type="submit" class="btn" style="margin-top:20px;width:100%;">Pay Now</button>
                </form>
            </div>
            '''
            return render_page("Yo! Payment", content, pc, pid, admin=False)

    elif active_method == 'iotec':
        return redirect(url_for('pay_iotec', pid=pid, plan_id=plan_id, phone=phone))

    elif active_method == 'pawapay':
        return redirect(url_for('pay_pawapay', pid=pid, plan_id=plan_id, phone=phone))

    elif active_method == 'pesapal':
        return redirect(url_for('pay_pesapal', pid=pid, plan_id=plan_id, phone=phone))

    else:
        # Fallback to manual
        content = f'''
        <div class="card">
            <div class="card-header">Pay for Internet</div>
            <p><strong>Selected Plan:</strong> {plan["name"]} – {plan["duration_minutes"]} min – UGX {plan["price_ugx"]:,}</p>
            <p><strong>Pay to:</strong></p>
            <p>MTN: {provider["mtn_number"] if provider and provider["mtn_number"] else 'N/A'} | Airtel: {provider["airtel_number"] if provider and provider["airtel_number"] else 'N/A'}</p>
            <p style="color:#666;">Name: {display_name}</p>
            <hr>
            <p style="margin-top:15px;"><strong>After payment, paste the full SMS below:</strong></p>
            <form method="POST">
                <input type="hidden" name="phone" value="{phone}">
                <input type="hidden" name="plan_id" value="{plan_id}">
                <input type="hidden" name="pid" value="{pid}">
                <label>Paste Full MTN/Airtel SMS Here</label>
                <textarea name="raw_sms" rows="6" required></textarea>
                <button type="submit" class="btn" style="margin-top:20px;width:100%;">Verify Payment</button>
            </form>
        </div>
        '''
        return render_page("Verify Payment", content, pc, pid, admin=False)


@app.route('/yo-pay')
def yo_pay():
    pid = request.args.get('pid', 1, type=int)
    phone = request.args.get('phone', '')
    plan_id = request.args.get('plan_id', '1')
    db = get_db()
    plan = db.execute("SELECT * FROM plans WHERE id=? AND provider_id=?", (plan_id, pid)).fetchone()
    if not plan:
        return "Invalid plan.", 400
    provider = get_provider(pid)
    result = yo_charge(phone, plan['price_ugx'], plan['name'], provider)
    if result == 'instant_success':
        return redirect("https://www.google.com")
    if result:
        return redirect(result)
    return render_page("Payment Error", f'<div class="card"><div class="alert alert-error">Automatic payment is unavailable. Please use manual payment.</div><p><a href="/sms-verify?phone={phone}&plan_id={plan_id}&pid={pid}" class="btn">Manual Payment</a></p></div>', get_pending_count(pid), pid, admin=False)


@app.route('/yo-callback', methods=['POST'])
def yo_callback():
    data = request.get_json()
    if data and data.get('transaction_status') == 'SUCCEEDED':
        tx_ref = data.get('external_ref')
        db = get_db()
        tx = db.execute("SELECT * FROM yo_tx WHERE tx_ref=? AND status='pending'", (tx_ref,)).fetchone()
        if tx:
            plan = db.execute("SELECT id, duration_minutes FROM plans WHERE provider_id=? AND price_ugx=? AND is_active=1 LIMIT 1", (tx['provider_id'], tx['amount'])).fetchone()
            if plan:
                code = generate_voucher_code()
                db.execute("INSERT INTO vouchers (provider_id,code,plan_id,payment_method,phone_number,used,used_at) VALUES (?,?,?,'yo',?,1,CURRENT_TIMESTAMP)", (tx['provider_id'], code, plan['id'], tx['phone']))
                db.execute("UPDATE yo_tx SET status='completed', voucher_code=? WHERE tx_ref=?", (code, tx_ref))
                db.commit()
                mt_add_user(tx['phone'], plan['duration_minutes'])
    return 'OK', 200


@app.route('/subscriber-login', methods=['GET','POST'])
def subscriber_login():
    pid = request.args.get('pid', 1, type=int)
    if request.method == 'POST':
        u = request.form['username'].strip()
        pw = request.form['password']
        db = get_db()
        sub = db.execute("SELECT id, password_hash, suspended FROM subscribers WHERE username=? AND provider_id=?", (u, pid)).fetchone()
        if sub and check_password_hash(sub['password_hash'], pw) and not sub['suspended']:
            db.execute("DELETE FROM sessions WHERE subscriber_id=?", (sub['id'],))
            ip = request.remote_addr
            db.execute("INSERT INTO sessions (subscriber_id, provider_id, ip_address) VALUES (?,?,?)", (sub['id'], pid, ip))
            db.execute("UPDATE subscribers SET current_ip=? WHERE id=?", (ip, sub['id']))
            db.commit()
            session['subscriber_id'] = sub['id']
            session['subscriber_name'] = u
            return redirect(url_for('subscriber_portal'))
        return render_page("Subscriber Login", '<div class="card"><div class="alert alert-error">Invalid credentials or account suspended.</div><a href="/subscriber-login?pid=' + str(pid) + '" class="btn">Try again</a></div>', get_pending_count(pid), pid, admin=False)
    return render_page("Subscriber Login", f'<div class="card"><div class="card-header">Subscriber Login</div><form method="POST"><input type="hidden" name="pid" value="{pid}"><label>Username</label><input type="text" name="username" required><label>Password</label><input type="password" name="password" required><button type="submit" class="btn" style="margin-top:20px;">Login</button></form></div>', get_pending_count(pid), pid, admin=False)


@app.route('/subscriber-portal')
def subscriber_portal():
    if 'subscriber_id' not in session:
        return redirect('/subscriber-login')
    pid = session.get('provider_id', 1)
    return render_page("Subscriber Portal", f'<div class="card"><h2>Welcome, {session["subscriber_name"]}</h2><p>You are connected. Your IP: {request.remote_addr}</p><a href="/subscriber-logout" class="btn btn-danger">Logout / Switch Device</a></div>', get_pending_count(pid), pid, admin=False)


@app.route('/subscriber-logout')
def subscriber_logout():
    if 'subscriber_id' in session:
        db = get_db()
        db.execute("DELETE FROM sessions WHERE subscriber_id=?", (session['subscriber_id'],))
        db.commit()
        session.pop('subscriber_id', None)
        session.pop('subscriber_name', None)
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
# PROVIDER DASHBOARD (Fancy Charts)
# ------------------------------------------------------------
@app.route('/dashboard')
@login_required
def dashboard():
    pid = session['provider_id']
    db = get_db()
    today = date.today()
    
    # ---------- STAT CARDS ----------
    # 1. Revenue this month
    month_start = today.replace(day=1).isoformat()
    amount_this_month = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM voucher_requests "
        "WHERE provider_id = ? AND status = 'approved' AND date(created_at) >= ?",
        (pid, month_start)
    ).fetchone()['total']
    
    # 2. Total unique clients (lifetime)
    total_clients = db.execute(
        "SELECT COUNT(DISTINCT phone_number) as total FROM vouchers WHERE provider_id = ?",
        (pid,)
    ).fetchone()['total']
    
    # 3. Active now (connected in last 15 minutes)
    active_now = db.execute(
        "SELECT COUNT(*) as active FROM vouchers "
        "WHERE provider_id = ? AND used = 1 AND used_at >= datetime('now', '-15 minutes')",
        (pid,)
    ).fetchone()['active']
    
    # 4. Active users in last 24 hours (unique)
    active_users_24h = db.execute(
        "SELECT COUNT(DISTINCT phone_number) as active FROM vouchers "
        "WHERE provider_id = ? AND used = 1 AND used_at >= datetime('now', '-1 day')",
        (pid,)
    ).fetchone()['active']
    
    # Growth compared to previous month
    last_month_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
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
    
    # ---------- PACKAGE PERFORMANCE TABLE ----------
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
        <tr>
            <td>{pr["name"]}</td>
            <td>UGX {pr["price_ugx"]:,}</td>
            <td>{pr["active_users"]}</td>
            <td>UGX {pr["monthly_rev"]:,}</td>
            <td>{format_data(pr["avg_data"])}</td>
            <td>UGX {arpu:,.0f}</td>
        </tr>'''
    if not pkg_rows:
        pkg_rows = '<tr><td colspan="6">No packages yet.</td></tr>'
    
    # ---------- RENDER CONTENT ----------
    content = f'''
    <!-- STAT CARDS -->
    <div class="stat-grid">
        <div class="stat-card">
            <h3>UGX {amount_this_month or 0:,}</h3>
            <small>Amount This Month</small>
            <div style="font-size:0.75rem; color:#28a745;">{growth_text}</div>
        </div>
        <div class="stat-card">
            <h3>{total_clients or 0}</h3>
            <small>Total Clients</small>
            <div style="font-size:0.75rem; color:var(--text-secondary);">🆕 Lifetime customers</div>
        </div>
        <div class="stat-card">
            <h3>{active_now or 0}</h3>
            <small>Active Now</small>
            <div style="font-size:0.75rem; color:#28a745;">🟢 Online right now</div>
        </div>
        <div class="stat-card">
            <h3>{active_users_24h or 0}</h3>
            <small>Active Users (24h)</small>
            <div style="font-size:0.75rem; color:var(--text-secondary);">📊 Daily active users</div>
        </div>
    </div>

    <!-- CHARTS ROW 1 -->
    <div class="chart-row">
        <div class="card">
            <div class="card-header">📊 Payments <select id="pp" onchange="loadPay()" style="width:auto;display:inline;">
                <option value="today">Today</option>
                <option value="this_week">This Week</option>
                <option value="last_week">Last Week</option>
                <option value="this_month" selected>This Month</option>
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

    <!-- CHARTS ROW 2 -->
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

    <!-- CHARTS ROW 3 -->
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

    <!-- CHARTS ROW 4 -->
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

    <!-- CHARTS ROW 5 -->
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

    <!-- PACKAGE PERFORMANCE TABLE -->
    <div class="card">
        <div class="card-header">🏆 Package Performance</div>
        <table>
            <thead><tr><th>Package</th><th>Price</th><th>Active</th><th>Monthly Rev</th><th>Avg Data</th><th>ARPU</th></tr></thead>
            <tbody>{pkg_rows}</tbody>
        </table>
    </div>

    <!-- JAVASCRIPT TO LOAD CHARTS -->
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

    // Initial load
    loadPay();
    </script>
    '''
    
    return render_page("Dashboard", content, get_pending_count(pid), pid, admin=True)

# ------------------------------------------------------------
# API ENDPOINTS (all use provider_id from session)
# ------------------------------------------------------------
@app.route('/api/payments')
@login_required
def api_payments():
    period = request.args.get('period', 'this_month')
    pid = session['provider_id']
    db = get_db()
    today = date.today()

    # Determine date range based on period
    if period == 'today':
        start_date = today
        end_date = today
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

    # Generate list of dates
    delta = end_date - start_date
    dates = [start_date + timedelta(days=i) for i in range(delta.days + 1)]
    labels = [d.strftime('%d %b') for d in dates]

    # Query payments per day
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
    pid = session['provider_id']
    db = get_db()
    today = date.today()
    labels = [(today - timedelta(days=i)).strftime('%a') for i in range(6, -1, -1)]

    values = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        # Count vouchers that are unused (active)
        v_cnt = db.execute(
            "SELECT COUNT(*) as c FROM vouchers WHERE provider_id = ? AND used = 0 AND date(created_at) <= ?",
            (pid, d.isoformat())
        ).fetchone()['c']
        # Count active sessions (started before end of day and not ended)
        s_cnt = db.execute(
            "SELECT COUNT(*) as c FROM sessions WHERE provider_id = ? AND date(started_at) <= ? AND (ended_at IS NULL OR date(ended_at) >= ?)",
            (pid, d.isoformat(), d.isoformat())
        ).fetchone()['c']
        values.append(v_cnt + s_cnt)

    return {'labels': labels, 'values': values}

@app.route('/api/retention')
@login_required
def api_retention():
    pid = session['provider_id']
    db = get_db()
    today = date.today()

    # We'll calculate for the last 6 months
    labels = [(today - timedelta(days=30 * i)).strftime('%b %Y') for i in range(5, -1, -1)]
    new_cust = []
    returning = []
    churned = []

    for i in range(5, -1, -1):
        month_start = (today - timedelta(days=30 * i)).replace(day=1)
        next_month = (month_start.replace(month=month_start.month % 12 + 1, day=1) if month_start.month < 12 else month_start.replace(year=month_start.year + 1, month=1, day=1))
        # New customers: first voucher purchase in this month
        new = db.execute(
            "SELECT COUNT(DISTINCT phone_number) as c FROM vouchers "
            "WHERE provider_id = ? AND date(created_at) >= ? AND date(created_at) < ? AND phone_number IS NOT NULL",
            (pid, month_start.isoformat(), next_month.isoformat())
        ).fetchone()['c']
        # Returning: customers who had a voucher in previous month and again in this month
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1)
        prev_month_end = month_start - timedelta(days=1)
        returning_count = db.execute(
            "SELECT COUNT(DISTINCT v2.phone_number) as c FROM vouchers v1 JOIN vouchers v2 ON v1.phone_number = v2.phone_number "
            "WHERE v1.provider_id = ? AND v2.provider_id = ? "
            "AND date(v1.created_at) >= ? AND date(v1.created_at) <= ? "
            "AND date(v2.created_at) >= ? AND date(v2.created_at) < ?",
            (pid, pid, prev_month_start.isoformat(), prev_month_end.isoformat(), month_start.isoformat(), next_month.isoformat())
        ).fetchone()['c']
        # Churned: customers who had a voucher in previous month but not this month
        churned_count = db.execute(
            "SELECT COUNT(DISTINCT phone_number) as c FROM vouchers v1 "
            "WHERE v1.provider_id = ? AND date(v1.created_at) >= ? AND date(v1.created_at) <= ? "
            "AND v1.phone_number NOT IN ("
            "    SELECT DISTINCT phone_number FROM vouchers v2 "
            "    WHERE v2.provider_id = ? AND date(v2.created_at) >= ? AND date(v2.created_at) < ?"
            ")",
            (pid, prev_month_start.isoformat(), prev_month_end.isoformat(), pid, month_start.isoformat(), next_month.isoformat())
        ).fetchone()['c']
        new_cust.append(new)
        returning.append(returning_count)
        churned.append(churned_count)

    return {'labels': labels, 'new_cust': new_cust, 'returning': returning, 'churned': churned}

@app.route('/api/data-usage')
@login_required
def api_data_usage():
    db = get_db(); today = date.today(); pid = session['provider_id']
    labels = [(today-timedelta(days=i)).strftime('%d') for i in range(29,-1,-1)]
    dl=[]; ul=[]
    for i in range(29,-1,-1):
        d = today-timedelta(days=i); row = db.execute("SELECT COALESCE(SUM(data_download),0) as dl, COALESCE(SUM(data_upload),0) as ul FROM data_sessions WHERE provider_id=? AND session_date=?",(pid,d.isoformat())).fetchone()
        dl.append(round(row['dl'],2)); ul.append(round(row['ul'],2))
    return {'labels':labels,'downloads':dl,'uploads':ul}

@app.route('/api/package-util')
@login_required
def api_package_util():
    pid = session['provider_id']
    db = get_db()
    # Count vouchers grouped by plan name for today
    rows = db.execute(
        "SELECT p.name, COUNT(v.id) as cnt FROM vouchers v JOIN plans p ON v.plan_id = p.id "
        "WHERE v.provider_id = ? AND date(v.created_at) = date('now') "
        "GROUP BY p.name ORDER BY cnt DESC",
        (pid,)
    ).fetchall()
    if not rows:
        return {'labels': ['No Sales'], 'values': [1]}
    labels = [r['name'] for r in rows]
    values = [r['cnt'] for r in rows]
    return {'labels': labels, 'values': values}
    
@app.route('/api/forecast')
@login_required
def api_forecast():
    pid = session['provider_id']
    db = get_db()
    today = date.today()

    # Historical: last 30 days
    hist_dates = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
    hist_values = []
    for d in hist_dates:
        total = db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM voucher_requests "
            "WHERE provider_id = ? AND status = 'approved' AND date(created_at) = ?",
            (pid, d.isoformat())
        ).fetchone()['total']
        hist_values.append(total)

    # Forecast: simple average of last 7 days projected for next 30 days
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

@app.route('/api/sms-stats')
@login_required
def api_sms_stats():
    db = get_db(); today = date.today(); pid = session['provider_id']
    labels = [(today-timedelta(days=i)).strftime('%a') for i in range(6,-1,-1)]
    vals = [db.execute("SELECT COUNT(*) as c FROM sms_log WHERE provider_id=? AND date(sent_at)=?",(pid,(today-timedelta(days=i)).isoformat())).fetchone()['c'] for i in range(6,-1,-1)]
    return {'labels':labels,'values':vals}

@app.route('/api/network')
@login_required
def api_network():
    db = get_db(); today = date.today(); pid = session['provider_id']
    labels = [(today-timedelta(days=i)).strftime('%a') for i in range(6,-1,-1)]
    dl=[]; ul=[]
    for i in range(6,-1,-1):
        d = today-timedelta(days=i); row = db.execute("SELECT COALESCE(SUM(data_download),0) as dl, COALESCE(SUM(data_upload),0) as ul FROM data_sessions WHERE provider_id=? AND session_date=?",(pid,d.isoformat())).fetchone()
        dl.append(round(row['dl'],2)); ul.append(round(row['ul'],2))
    return {'labels':labels,'downloads':dl,'uploads':ul}

@app.route('/api/registration')
@login_required
def api_registration():
    db = get_db(); today = date.today(); pid = session['provider_id']
    labels = [(today-timedelta(days=i)).strftime('%a') for i in range(6,-1,-1)]
    vals = [db.execute("SELECT COUNT(*) as c FROM user_activity WHERE provider_id=? AND action='voucher_purchased' AND date(created_at)=?",(pid,(today-timedelta(days=i)).isoformat())).fetchone()['c'] for i in range(6,-1,-1)]
    return {'labels':labels,'values':vals}

@app.route('/api/most-active')
@login_required
def api_most_active():
    db = get_db(); pid = session['provider_id']
    rows = db.execute("SELECT phone_number, COALESCE(SUM(data_download+data_upload),0) as t FROM data_sessions WHERE provider_id=? GROUP BY phone_number ORDER BY t DESC LIMIT 10",(pid,)).fetchall()
    return [{'username':r['phone_number'][:7]+'...','phone':r['phone_number'],'data_usage':format_data(r['t'])} for r in rows]

@app.route('/api/package-perf')
@login_required
def api_package_perf():
    db = get_db(); pid = session['provider_id']
    plans = db.execute("SELECT id,name,price_ugx FROM plans WHERE provider_id=? AND is_active=1",(pid,)).fetchall()
    labels = [p['name'] for p in plans]; sales=[]; rev=[]
    for p in plans:
        c = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE plan_id=? AND provider_id=?",(p['id'],pid)).fetchone()['c']
        sales.append(c); rev.append(c*p['price_ugx'])
    return {'labels':labels,'sales':sales,'revenue':rev}

# ------------------------------------------------------------
# ACTIVE USERS
# ------------------------------------------------------------
@app.route('/active-users')
@login_required
def active_users():
    pid = session['provider_id']
    db = get_db()
    
    # Get live data from MikroTik
    live = mt_get_active_users(pid)
    hotspot_users = live.get('hotspot', [])
    ppp_users = live.get('ppp', [])
    
    rows = ''
    
    # Hotspot users
    for u in hotspot_users:
        username = u['username']
        rows += f'''
        <tr>
            <td>{username}</td>
            <td>{u['ip']}</td>
            <td>{u['mac']}</td>
            <td>Hotspot</td>
            <td>{u['uptime']}</td>
            <td>
                <a href="/disconnect-active/{username}?type=hotspot" class="btn btn-small btn-danger" onclick="return confirm('Disconnect {username}?')">Disconnect</a>
            </td>
        </tr>
        '''
    
    # PPPoE users
    for u in ppp_users:
        username = u['username']
        rows += f'''
        <tr>
            <td>{username}</td>
            <td>{u['ip']}</td>
            <td>{u['mac']}</td>
            <td>PPPoE</td>
            <td>{u['uptime']}</td>
            <td>
                <a href="/disconnect-active/{username}?type=ppp" class="btn btn-small btn-danger" onclick="return confirm('Disconnect {username}?')">Disconnect</a>
            </td>
        </tr>
        '''
    
    if not rows:
        rows = '<tr><td colspan="6">No active users found on the router(s).</td></tr>'
    
    content = f'''
    <div class="card">
        <div class="card-header">
            🔴 Active Users (Live)
            <span class="badge" style="background:#28a745; color:#fff;">{len(hotspot_users) + len(ppp_users)} online</span>
            <a href="/active-users" class="btn btn-small" style="float:right;">🔄 Refresh</a>
        </div>
        <div class="table-responsive" style="overflow-x:auto;">
            <table>
                <thead>
                    <tr>
                        <th>Username</th>
                        <th>IP Address</th>
                        <th>MAC Address</th>
                        <th>Type</th>
                        <th>Uptime</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    <script>
        // Auto-refresh every 30 seconds
        setTimeout(function() {{
            location.reload();
        }}, 30000);
    </script>
    '''
    
    return render_page("Active Users", content, get_pending_count(pid), pid, admin=True)
    
@app.route('/disconnect-active/<username>')
@login_required
def disconnect_active(username):
    # Get the type (hotspot or ppp) from the query string
    user_type = request.args.get('type', 'hotspot')
    api = mt_connect()
    if not api:
        return "Could not connect to MikroTik", 500
    
    try:
        if user_type == 'hotspot':
            api(cmd='/ip/hotspot/active/remove', **{'user': username})
        elif user_type == 'ppp':
            api(cmd='/ppp/active/remove', **{'name': username})
        else:
            return "Invalid type", 400
    except Exception as e:
        return f"Error disconnecting: {e}", 500
    finally:
        api.close()
    
    # Also optionally update the database (mark voucher as used, etc.)
    # but the router is the source of truth.
    return redirect(url_for('active_users'))

@app.route('/disconnect-voucher/<int:vid>')
@login_required
def disconnect_voucher(vid):
    db = get_db(); v = db.execute("SELECT phone_number FROM vouchers WHERE id=?",(vid,)).fetchone()
    if v: mt_remove_user(v['phone_number']); db.execute("UPDATE vouchers SET used=1, used_at=CURRENT_TIMESTAMP WHERE id=?",(vid,)); db.commit()
    return redirect('/active-users')

@app.route('/disconnect-voucher-until-payment/<int:vid>')
@login_required
def disconnect_voucher_until_payment(vid):
    db = get_db(); v = db.execute("SELECT phone_number FROM vouchers WHERE id=?",(vid,)).fetchone()
    if v: db.execute("INSERT OR IGNORE INTO restricted (provider_id,phone_number,reason) VALUES (?,'until payment',?)",(session['provider_id'],v['phone_number'])); db.execute("UPDATE vouchers SET used=1, used_at=CURRENT_TIMESTAMP WHERE id=?",(vid,)); db.commit()
    return redirect('/active-users')

@app.route('/disconnect-subscriber/<int:sid>')
@login_required
def disconnect_subscriber(sid):
    db = get_db(); db.execute("DELETE FROM sessions WHERE id=? AND provider_id=?",(sid,session['provider_id'])); db.commit()
    return redirect('/active-users')

@app.route('/suspend-subscriber/<int:sid>')
@login_required
def suspend_subscriber(sid):
    db = get_db(); s = db.execute("SELECT subscriber_id FROM sessions WHERE id=?",(sid,)).fetchone()
    if s: db.execute("UPDATE subscribers SET suspended=1 WHERE id=?",(s['subscriber_id'],)); db.execute("DELETE FROM sessions WHERE id=?",(sid,)); db.commit()
    return redirect('/active-users')

# ------------------------------------------------------------
# USERS
# ------------------------------------------------------------
@app.route('/users')
@login_required
def users_list():
    pid = session['provider_id']; db = get_db()
    subs = db.execute("SELECT * FROM subscribers WHERE provider_id=?",(pid,)).fetchall()
    rows = ''
    for s in subs:
        rows += f'<tr><td>{s["username"]}</td><td>{s["phone"] or "-"}</td><td>{s["package_name"] or "-"}</td><td>{s["expiry_date"] if s["expiry_date"] else "-"}</td><td>{s["created_at"]}</td></tr>'
    if not rows: rows = '<tr><td colspan="5">No users found.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Users <a href="/subscribers" class="btn btn-success btn-small">Create User</a></div>
    <table><thead><tr><th>Username</th><th>Phone</th><th>Package</th><th>Expiry</th><th>Last Online</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Users", content, get_pending_count(pid), pid, admin=True)

@app.route('/subscribers', methods=['GET','POST'])
@login_required
def subscribers():
    pid = session['provider_id']; db = get_db()
    if request.method == 'POST':
        u = request.form['username'].strip(); pw = request.form['password']; ph = request.form.get('phone','').strip()
        pkg_id = request.form.get('package_id',''); expiry = request.form.get('expiry_date','')
        pkg_name = db.execute("SELECT name FROM plans WHERE id=?",(pkg_id,)).fetchone()
        try: db.execute("INSERT INTO subscribers (provider_id,username,password_hash,phone,package_name,expiry_date) VALUES (?,?,?,?,?,?)",(pid,u,generate_password_hash(pw),ph,pkg_name['name'] if pkg_name else '',expiry)); db.commit()
        except: return render_page("Users",'<div class="card"><div class="alert alert-error">Username already exists.</div><p><a href="/users">Back</a></p></div>', get_pending_count(pid), pid, admin=True)
        return redirect('/users')
    pkg_opts = get_plan_options(pid, public_only=False)
    return render_page("Create User",f'''<div class="card"><div class="card-header">Create User</div>
    <form method="POST"><label>Username</label><input type="text" name="username" required><label>Password</label><input type="password" name="password" required><label>Phone (optional)</label><input type="tel" name="phone">
    <label>Package*</label><select name="package_id" required>{pkg_opts}</select><a href="/plans/add" target="_blank" style="font-size:0.8rem;">+ Add new package</a>
    <label>Expiry Date</label><input type="date" name="expiry_date"><button type="submit" class="btn btn-success" style="margin-top:15px;">Create Subscriber</button></form></div>''', get_pending_count(pid), pid, admin=True)

@app.route('/delete-subscriber/<int:sid>')
@login_required
def delete_subscriber(sid):
    db = get_db(); db.execute("DELETE FROM subscribers WHERE id=? AND provider_id=?",(sid,session['provider_id'])); db.execute("DELETE FROM sessions WHERE subscriber_id=?",(sid,)); db.commit()
    return redirect('/users')

# ------------------------------------------------------------
# EXPIRY DATES
# ------------------------------------------------------------
@app.route('/expiry-dates')
@login_required
def expiry_dates():
    pid = session['provider_id']; db = get_db()
    items = db.execute("SELECT e.*, s.username FROM expiry_dates e JOIN subscribers s ON e.user_id=s.id WHERE e.provider_id=? ORDER BY e.expiry_date DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{e["username"]}</td><td>{e["expiry_date"]}</td><td>{e["grace_period"]}</td><td><a href="/expiry-dates/edit/{e["id"]}" class="btn btn-small">Edit</a> <a href="/expiry-dates/delete/{e["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for e in items) or '<tr><td colspan="4">No expiry dates set.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Expiry Dates <a href="/expiry-dates/add" class="btn btn-success btn-small">Create Expiry Date</a></div>
    <table><thead><tr><th>User</th><th>Expiry Date</th><th>Grace Period</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Expiry Dates", content, get_pending_count(pid), pid, admin=True)

@app.route('/expiry-dates/add', methods=['GET','POST'])
@login_required
def add_expiry_date():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db()
        db.execute("INSERT INTO expiry_dates (provider_id, user_id, expiry_date, grace_period) VALUES (?,?,?,?)",
                   (pid, int(request.form['user_id']), request.form['expiry_date'], int(request.form.get('grace', 0))))
        db.commit()
        return redirect('/expiry-dates')
    db = get_db()
    users = db.execute("SELECT id, username FROM subscribers WHERE provider_id=?", (pid,)).fetchall()
    user_opts = ''.join(f'<option value="{u["id"]}">{u["username"]}</option>' for u in users)
    return render_page("Create Expiry Date", f'''
    <div class="card">
        <div class="card-header">Create Expiry Date</div>
        <form method="POST">
            <label>User*</label><select name="user_id" required>{user_opts}</select>
            <div style="display:flex;gap:10px;margin-top:10px;flex-wrap:wrap;">
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry_date]').value='{(datetime.now()+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}'">+ 1 Hour</button>
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry_date]').value='{(datetime.now()+timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M')}'">+ 12 Hours</button>
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry_date]').value='{(datetime.now()+timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')}'">+ 1 Day</button>
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry_date]').value='{(datetime.now()+timedelta(days=7)).strftime('%Y-%m-%dT%H:%M')}'">+ 7 Days</button>
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry_date]').value='{(datetime.now()+timedelta(days=30)).strftime('%Y-%m-%dT%H:%M')}'">+ 1 Month</button>
            </div>
            <label>Expiry Date*</label><input type="datetime-local" name="expiry_date" required>
            <label>Grace Period (days)</label><input type="number" name="grace" value="0">
            <button type="submit" class="btn" style="margin-top:20px;">Create</button>
        </form>
    </div>
    ''', get_pending_count(pid), pid, admin=True)

@app.route('/expiry-dates/edit/<int:eid>', methods=['GET','POST'])
@login_required
def edit_expiry_date(eid):
    pid = session['provider_id']
    db = get_db()
    if request.method == 'POST':
        db.execute("UPDATE expiry_dates SET expiry_date=?, grace_period=? WHERE id=? AND provider_id=?",
                   (request.form['expiry_date'], int(request.form.get('grace', 0)), eid, pid))
        db.commit()
        return redirect('/expiry-dates')
    e = db.execute("SELECT * FROM expiry_dates WHERE id=? AND provider_id=?", (eid, pid)).fetchone()
    if not e:
        return "Not found", 404
    users = db.execute("SELECT id, username FROM subscribers WHERE provider_id=?", (pid,)).fetchall()
    user_opts = ''.join(f'<option value="{u["id"]}" {"selected" if u["id"]==e["user_id"] else ""}>{u["username"]}</option>' for u in users)
    return render_page("Edit Expiry Date", f'''
    <div class="card">
        <div class="card-header">Edit Expiry Date</div>
        <form method="POST">
            <label>User</label><select name="user_id">{user_opts}</select>
            <label>Expiry Date</label><input type="datetime-local" name="expiry_date" value="{e['expiry_date']}" required>
            <label>Grace Period (days)</label><input type="number" name="grace" value="{e['grace_period']}">
            <button type="submit" class="btn" style="margin-top:20px;">Update</button>
        </form>
    </div>
    ''', get_pending_count(pid), pid, admin=True)

@app.route('/expiry-dates/delete/<int:eid>')
@login_required
def delete_expiry_date(eid): db = get_db(); db.execute("DELETE FROM expiry_dates WHERE id=? AND provider_id=?",(eid,session['provider_id'])); db.commit(); return redirect('/expiry-dates')

# ------------------------------------------------------------
# IP BINDINGS
# ------------------------------------------------------------
@app.route('/ip-bindings')
@login_required
def ip_bindings():
    pid = session['provider_id']; db = get_db()
    items = db.execute("SELECT * FROM ip_bindings WHERE provider_id=? ORDER BY id DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{i["name"]}</td><td>{i["mikrotik_id"] or "-"}</td><td>{i["address"]}</td><td>{i["package_id"] or "-"}</td><td>{i["expires_at"] or "-"}</td><td><a href="/ip-bindings/edit/{i["id"]}" class="btn btn-small">Edit</a> <a href="/ip-bindings/delete/{i["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for i in items) or '<tr><td colspan="6">No IP bindings.</td></tr>'
    content = f'''<div class="card"><div class="card-header">IP Bindings <a href="/ip-bindings/add" class="btn btn-success btn-small">Bind IP</a></div>
    <table><thead><tr><th>Name</th><th>MikroTik</th><th>Address</th><th>Package</th><th>Expiry</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("IP Bindings", content, get_pending_count(pid), pid, admin=True)

@app.route('/ip-bindings/add', methods=['GET','POST'])
@login_required
def add_ip_binding():
    pid = session['provider_id']
    db = get_db()
    
    if request.method == 'POST':
        db.execute("""INSERT INTO ip_bindings 
                   (provider_id, mikrotik_id, name, package_id, dhcp_lease, address, mac_address, expires_at) 
                   VALUES (?,?,?,?,?,?,?,?)""",
                   (pid,
                    int(request.form.get('mikrotik_id', 0)),
                    request.form['name'],
                    int(request.form.get('package_id', 0)),
                    request.form.get('dhcp', ''),
                    request.form['address'],
                    request.form['mac'],
                    request.form.get('expires_at')))
        db.commit()
        return redirect('/ip-bindings')
    
    routers = db.execute("SELECT id, name FROM mikrotik_routers WHERE provider_id=?", (pid,)).fetchall()
    router_opts = ''.join(f'<option value="{r["id"]}">{r["name"]}</option>' for r in routers)
    
    packages = db.execute("SELECT id, name FROM plans WHERE provider_id=? AND is_active=1", (pid,)).fetchall()
    pkg_opts = ''.join(f'<option value="{p["id"]}">{p["name"]}</option>' for p in packages)
    
    content = f'''<div class="card"><div class="card-header">Bind an IP</div>
    <form method="POST">
        <label>MikroTik*</label><select name="mikrotik_id">{router_opts}</select>
        <label>Name*</label><input type="text" name="name" required>
        <label>Package</label><select name="package_id"><option value="">None</option>{pkg_opts}</select>
        <label>DHCP Lease</label><input type="text" name="dhcp" placeholder="Select a DHCP lease to auto-fill">
        <label>Address*</label><input type="text" name="address" required>
        <label>MAC Address*</label><input type="text" name="mac" required>
        <label>Expires At (Optional)</label><input type="datetime-local" name="expires_at">
        <button type="submit" class="btn" style="margin-top:20px;">Create</button>
    </form></div>'''
    return render_page("Bind an IP", content, get_pending_count(pid), pid, admin=True)

@app.route('/ip-bindings/edit/<int:bid>', methods=['GET','POST'])
@login_required
def edit_ip_binding(bid):
    pid = session['provider_id']
    db = get_db()
    
    if request.method == 'POST':
        db.execute("""UPDATE ip_bindings SET 
                   mikrotik_id=?, name=?, package_id=?, dhcp_lease=?, address=?, mac_address=?, expires_at=? 
                   WHERE id=? AND provider_id=?""",
                   (int(request.form.get('mikrotik_id', 0)),
                    request.form['name'],
                    int(request.form.get('package_id', 0)),
                    request.form.get('dhcp', ''),
                    request.form['address'],
                    request.form['mac'],
                    request.form.get('expires_at'),
                    bid, pid))
        db.commit()
        return redirect('/ip-bindings')
    
    b = db.execute("SELECT * FROM ip_bindings WHERE id=? AND provider_id=?", (bid, pid)).fetchone()
    if not b:
        return "Not found", 404
    
    routers = db.execute("SELECT id, name FROM mikrotik_routers WHERE provider_id=?", (pid,)).fetchall()
    router_opts = ''.join(f'<option value="{r["id"]}" {"selected" if r["id"]==b["mikrotik_id"] else ""}>{r["name"]}</option>' for r in routers)
    
    packages = db.execute("SELECT id, name FROM plans WHERE provider_id=? AND is_active=1", (pid,)).fetchall()
    pkg_opts = ''.join(f'<option value="{p["id"]}" {"selected" if p["id"]==b["package_id"] else ""}>{p["name"]}</option>' for p in packages)
    
    content = f'''<div class="card"><div class="card-header">Edit IP Binding</div>
    <form method="POST">
        <label>MikroTik</label><select name="mikrotik_id">{router_opts}</select>
        <label>Name</label><input type="text" name="name" value="{b["name"]}" required>
        <label>Package</label><select name="package_id"><option value="">None</option>{pkg_opts}</select>
        <label>Address</label><input type="text" name="address" value="{b["address"]}" required>
        <label>MAC Address</label><input type="text" name="mac" value="{b["mac_address"]}" required>
        <label>Expires At</label><input type="datetime-local" name="expires_at" value="{b["expires_at"] if b["expires_at"] else ''}">
        <button type="submit" class="btn" style="margin-top:20px;">Update</button>
    </form></div>'''
    return render_page("Edit IP Binding", content, get_pending_count(pid), pid, admin=True)

@app.route('/ip-bindings/delete/<int:bid>')
@login_required
def delete_ip_binding(bid): db = get_db(); db.execute("DELETE FROM ip_bindings WHERE id=? AND provider_id=?",(bid,session['provider_id'])); db.commit(); return redirect('/ip-bindings')

# ------------------------------------------------------------
# TICKETS
# ------------------------------------------------------------
@app.route('/tickets')
@login_required
def tickets():
    pid = session['provider_id']; db = get_db(); items = db.execute("SELECT * FROM tickets WHERE provider_id=? ORDER BY id DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{t["subject"]}</td><td>TICKET-{t["id"]}</td><td>{t["status"]}</td><td>{t["priority"]}</td><td>{t["created_at"][:16] if t["created_at"] else ""}</td><td><a href="/tickets/edit/{t["id"]}" class="btn btn-small">Edit</a> <a href="/tickets/delete/{t["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for t in items) or '<tr><td colspan="6">No tickets have been raised yet.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Tickets <a href="/tickets/add" class="btn btn-success btn-small">Raise Ticket</a></div>
    <div class="tabs"><span class="tab active">Open Tickets</span><span class="tab">Closed Tickets</span></div>
    <table><thead><tr><th>Client</th><th>Ticket #</th><th>Status</th><th>Priority</th><th>Created at</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Tickets", content, get_pending_count(pid), pid, admin=True)

@app.route('/tickets/add', methods=['GET','POST'])
@login_required
def add_ticket():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db()
        db.execute("INSERT INTO tickets (provider_id, subject, description, priority, status) VALUES (?,?,?,?,?)",
                   (pid, request.form['subject'], request.form['description'], request.form['priority'], request.form['status']))
        db.commit()
        return redirect('/tickets')
    return render_page("Raise Ticket", '''
    <div class="card">
        <div class="card-header">Raise Ticket</div>
        <form method="POST">
            <label>Client*</label><input type="text" name="client" required>
            <label>Subject*</label><input type="text" name="subject" required>
            <label>Status</label><select name="status"><option value="open">Open</option><option value="closed">Closed</option></select>
            <label>Priority</label><select name="priority"><option value="medium">Medium</option><option value="high">High</option><option value="low">Low</option></select>
            <label>Description*</label><textarea name="description" required></textarea>
            <button type="submit" class="btn" style="margin-top:20px;">Create</button>
        </form>
    </div>
    ''', get_pending_count(pid), pid, admin=True)

@app.route('/tickets/edit/<int:tid>', methods=['GET','POST'])
@login_required
def edit_ticket(tid):
    pid = session['provider_id']
    db = get_db()
    if request.method == 'POST':
        db.execute("UPDATE tickets SET subject=?, description=?, status=?, priority=? WHERE id=? AND provider_id=?",
                   (request.form['subject'], request.form['description'], request.form['status'], request.form['priority'], tid, pid))
        db.commit()
        return redirect('/tickets')
    t = db.execute("SELECT * FROM tickets WHERE id=? AND provider_id=?", (tid, pid)).fetchone()
    if not t:
        return "Not found", 404
    content = f'''
    <div class="card">
        <div class="card-header">Edit Ticket</div>
        <form method="POST">
            <label>Subject</label><input type="text" name="subject" value="{t['subject']}" required>
            <label>Status</label><select name="status">
                <option value="open" {"selected" if t['status']=='open' else ""}>Open</option>
                <option value="closed" {"selected" if t['status']=='closed' else ""}>Closed</option>
            </select>
            <label>Priority</label><select name="priority">
                <option value="medium" {"selected" if t['priority']=='medium' else ""}>Medium</option>
                <option value="high" {"selected" if t['priority']=='high' else ""}>High</option>
                <option value="low" {"selected" if t['priority']=='low' else ""}>Low</option>
            </select>
            <label>Description</label><textarea name="description">{t['description'] or ''}</textarea>
            <button type="submit" class="btn" style="margin-top:20px;">Update</button>
        </form>
    </div>
    '''
    return render_page("Edit Ticket", content, get_pending_count(pid), pid, admin=True)

@app.route('/tickets/delete/<int:tid>')
@login_required
def delete_ticket(tid): db = get_db(); db.execute("DELETE FROM tickets WHERE id=? AND provider_id=?",(tid,session['provider_id'])); db.commit(); return redirect('/tickets')

# ------------------------------------------------------------
# LEADS
# ------------------------------------------------------------
@app.route('/leads')
@login_required
def leads():
    pid = session['provider_id']; db = get_db(); items = db.execute("SELECT * FROM leads WHERE provider_id=? ORDER BY id DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{l["name"]}</td><td>{l["email"] or "-"}</td><td>{l["phone"] or "-"}</td><td>{l["source"] or "-"}</td><td><a href="/leads/edit/{l["id"]}" class="btn btn-small">Edit</a> <a href="/leads/delete/{l["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for l in items) or '<tr><td colspan="5">No leads yet.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Leads <a href="/leads/add" class="btn btn-success btn-small">Create a new lead</a></div>
    <table><thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Address</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Leads", content, get_pending_count(pid), pid, admin=True)

@app.route('/leads/add', methods=['GET','POST'])
@login_required
def add_lead():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db()
        db.execute("INSERT INTO leads (provider_id, name, phone, email, source) VALUES (?,?,?,?,?)",
                   (pid, request.form['name'], request.form['phone'], request.form['email'], request.form['address']))
        db.commit()
        return redirect('/leads')
    return render_page("Create Lead", '''
    <div class="card">
        <div class="card-header">Create a new lead</div>
        <form method="POST">
            <label>Name*</label><input type="text" name="name" required>
            <label>Email</label><input type="email" name="email">
            <label>Phone*</label><input type="tel" name="phone" required>
            <label>Address*</label><input type="text" name="address" required>
            <button type="submit" class="btn" style="margin-top:20px;">Create</button>
        </form>
    </div>
    ''', get_pending_count(pid), pid, admin=True)

@app.route('/leads/edit/<int:lid>', methods=['GET','POST'])
@login_required
def edit_lead(lid):
    pid = session['provider_id']
    db = get_db()
    if request.method == 'POST':
        db.execute("UPDATE leads SET name=?, phone=?, email=?, source=? WHERE id=? AND provider_id=?",
                   (request.form['name'], request.form['phone'], request.form['email'], request.form['address'], lid, pid))
        db.commit()
        return redirect('/leads')
    l = db.execute("SELECT * FROM leads WHERE id=? AND provider_id=?", (lid, pid)).fetchone()
    if not l:
        return "Not found", 404
    content = f'''
    <div class="card">
        <div class="card-header">Edit Lead</div>
        <form method="POST">
            <label>Name*</label><input type="text" name="name" value="{l['name']}" required>
            <label>Email</label><input type="email" name="email" value="{l['email'] or ''}">
            <label>Phone*</label><input type="tel" name="phone" value="{l['phone'] or ''}" required>
            <label>Address*</label><input type="text" name="address" value="{l['source'] or ''}" required>
            <button type="submit" class="btn" style="margin-top:20px;">Update</button>
        </form>
    </div>
    '''
    return render_page("Edit Lead", content, get_pending_count(pid), pid, admin=True)

@app.route('/leads/delete/<int:lid>')
@login_required
def delete_lead(lid): db = get_db(); db.execute("DELETE FROM leads WHERE id=? AND provider_id=?",(lid,session['provider_id'])); db.commit(); return redirect('/leads')

# ------------------------------------------------------------
# PACKAGES (Plans)
# ------------------------------------------------------------
@app.route('/plans')
@login_required
def list_plans():
    pid = session['provider_id']; db = get_db(); plans = db.execute("SELECT * FROM plans WHERE provider_id=? AND is_public=1",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{p["name"]}</td><td>UGX {p["price_ugx"]:,}</td><td>{p["speed_down"] or "-"}/{p["speed_up"] or "-"}</td><td>{p["duration_minutes"]} min</td><td>Hotspot</td><td>1</td><td>{"Yes" if p["is_active"] else "No"}</td><td><a href="/plans/edit/{p["id"]}" class="btn btn-small">Edit</a> <a href="/plans/delete/{p["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for p in plans) or '<tr><td colspan="8">No packages yet.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Packages <a href="/plans/add" class="btn btn-success btn-small">Create Package</a></div>
    <table><thead><tr><th>Name</th><th>Price</th><th>Speed</th><th>Time</th><th>Type</th><th>Devices</th><th>Enabled</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Packages", content, get_pending_count(pid), pid, admin=True)

@app.route('/plans/add', methods=['GET','POST'])
@login_required
def add_plan():
    if request.method == 'POST':
        db = get_db(); db.execute("INSERT INTO plans (provider_id,name,duration_minutes,price_ugx,is_public,speed_down,speed_up) VALUES (?,?,?,?,1,?,?)",(session['provider_id'],request.form['name'],int(request.form['duration']),int(request.form['price']),request.form.get('speed_down',''),request.form.get('speed_up',''))); db.commit()
        return redirect('/plans')
    return render_page("Create Package",'''<div class="card"><div class="card-header">Create Package</div>
    <form method="POST"><label>Name of package*</label><input type="text" name="name" required><label>Duration (minutes)*</label><input type="number" name="duration" required><label>Price (UGX)*</label><input type="number" name="price" required><label>Download Speed</label><input type="text" name="speed_down" placeholder="e.g. 5M"><label>Upload Speed</label><input type="text" name="speed_up" placeholder="e.g. 2M"><button type="submit" class="btn" style="margin-top:20px;">Create</button></form></div>''', get_pending_count(pid), pid, admin=True)

@app.route('/plans/edit/<int:plid>', methods=['GET','POST'])
@login_required
def edit_plan(plid):
    db = get_db(); plan = db.execute("SELECT * FROM plans WHERE id=? AND provider_id=?",(plid,session['provider_id'])).fetchone()
    if not plan: return "Not found", 404
    if request.method == 'POST':
        db.execute("UPDATE plans SET name=?,duration_minutes=?,price_ugx=?,is_active=?,speed_down=?,speed_up=? WHERE id=?",(request.form['name'],int(request.form['duration']),int(request.form['price']),int(request.form.get('is_active','1')),request.form.get('speed_down',''),request.form.get('speed_up',''),plid)); db.commit()
        return redirect('/plans')
    content = f'<div class="card"><div class="card-header">Edit Package</div><form method="POST"><label>Name</label><input type="text" name="name" value="{plan["name"]}" required><label>Duration</label><input type="number" name="duration" value="{plan["duration_minutes"]}" required><label>Price</label><input type="number" name="price" value="{plan["price_ugx"]}" required><label>Download Speed</label><input type="text" name="speed_down" value="{plan["speed_down"] or ""}"><label>Upload Speed</label><input type="text" name="speed_up" value="{plan["speed_up"] or ""}"><label>Active</label><select name="is_active"><option value="1" {"selected" if plan["is_active"] else ""}>Yes</option><option value="0" {"selected" if not plan["is_active"] else ""}>No</option></select><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Package", content, get_pending_count(pid), pid, admin=True)

@app.route('/plans/delete/<int:plid>')
@login_required
def delete_plan(plid): db = get_db(); db.execute("DELETE FROM plans WHERE id=? AND provider_id=?",(plid,session['provider_id'])); db.commit(); return redirect('/plans')

# ------------------------------------------------------------
# PAYMENTS
# ------------------------------------------------------------
@app.route('/payments')
@login_required
def payments():
    pid = session['provider_id']; db = get_db()
    today = date.today().isoformat()
    daily = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at)=?",(pid,today)).fetchone()['t']
    weekly = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at) >= ?",(pid,(date.today()-timedelta(days=7)).isoformat())).fetchone()['t']
    monthly = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at) >= ?",(pid,date.today().replace(day=1).isoformat())).fetchone()['t']
    items = db.execute("SELECT vr.*, COALESCE(sub.username, vr.phone_number) as user_name FROM voucher_requests vr LEFT JOIN subscribers sub ON sub.phone=vr.phone_number WHERE vr.provider_id=? ORDER BY vr.created_at DESC LIMIT 50",(pid,)).fetchall()
    rows = ''
    for i in items:
        checked = "Yes" if i['status'] == 'approved' else "No"
        rows += f'<tr><td>{i["user_name"]}</td><td>{i["phone_number"]}</td><td>{i["transaction_id"]}</td><td>UGX {i["amount"] or 0:,}</td><td>{checked}</td><td>{i["created_at"][:16] if i["created_at"] else ""}</td>'
        if i['status'] == 'pending': rows += f'<td><a href="/approve/{i["id"]}" class="btn btn-small btn-success">Approve</a> <a href="/reject/{i["id"]}" class="btn btn-small btn-danger">Reject</a></td>'
        else: rows += '<td>-</td>'
        rows += '</tr>'
    if not rows: rows = '<tr><td colspan="7">No payments yet.</td></tr>'
    content = f'''<div class="stat-grid"><div class="stat-card"><h3>UGX {daily or 0:,}</h3><small>Daily Earnings</small></div><div class="stat-card"><h3>UGX {weekly or 0:,}</h3><small>Weekly Earnings</small></div><div class="stat-card"><h3>UGX {monthly or 0:,}</h3><small>Monthly Earnings</small></div></div>
    <div class="card"><div class="card-header">Payments <a href="/record-payment" class="btn btn-success btn-small">Record Payment</a></div>
    <div class="tabs"><a href="/payments?filter=all" class="tab active">All</a><a href="/payments?filter=checked" class="tab">Checked</a><a href="/payments?filter=unchecked" class="tab">Unchecked</a></div>
    <table><thead><tr><th>User</th><th>Phone</th><th>Receipt No.</th><th>Amount</th><th>Checked</th><th>Paid At</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Payments", content, get_pending_count(pid), pid, admin=True)
    
@app.route('/record-payment', methods=['GET','POST'])
@login_required
def record_payment():
    # Get provider_id from the session (available after login)
    provider_id = session['provider_id']

    if request.method == 'POST':
        db = get_db()
        # Insert a manually recorded payment (approve instantly)
        db.execute("""
            INSERT INTO voucher_requests 
            (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status)
            VALUES (?, 'manual', 1, 'manual', ?, ?, 'manual', date('now'), 'approved')
        """, (provider_id, request.form['receipt'], float(request.form['amount'])))
        db.commit()
        return redirect('/payments')

    # GET request – render the payment form
    html = '''
    <div class="card">
        <div class="card-header">Record Payment</div>
        <form method="POST">
            <label>Receipt Number*</label>
            <input type="text" name="receipt" required>
            <label>Amount (UGX)*</label>
            <input type="number" name="amount" step="0.01" required>
            <button type="submit" class="btn" style="margin-top:20px;">Create</button>
        </form>
    </div>
    '''
    return render_page(
        "Record Payment",
        html,
        get_pending_count(provider_id),   # fix: use provider_id
        provider_id,                      # fix: use provider_id
        admin=True
    )

@app.route('/approve/<int:rid>')
@login_required
def approve(rid):
    pid = session['provider_id']
    db = get_db()
    r = db.execute("SELECT phone_number, plan_id FROM voucher_requests WHERE id=? AND provider_id=?", (rid, pid)).fetchone()
    if r:
        plan = db.execute("SELECT duration_minutes FROM plans WHERE id=?", (r['plan_id'],)).fetchone()
        if plan:
            code = generate_voucher_code()
            # Insert voucher as used immediately
            db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number, used, used_at) VALUES (?,?,?,'sms',?,1,CURRENT_TIMESTAMP)",
                       (pid, code, r['plan_id'], r['phone_number']))
            db.execute("UPDATE voucher_requests SET status='approved', voucher_code=? WHERE id=?", (code, rid))
            db.commit()
            # Add user to MikroTik now
            mt_add_user(r['phone_number'], plan['duration_minutes'])
    return redirect('/payments')

@app.route('/reject/<int:rid>')
@login_required
def reject(rid):
    pid = session['provider_id']; db = get_db(); db.execute("UPDATE voucher_requests SET status='rejected' WHERE id=? AND provider_id=?",(rid,pid)); db.commit()
    return redirect('/payments')

# ------------------------------------------------------------
# VOUCHERS
# ------------------------------------------------------------
@app.route('/vouchers')
@login_required
def vouchers_list():
    pid = session['provider_id']; db = get_db()
    filter_type = request.args.get('filter', 'all')
    query = "SELECT v.code, p.name as pn, v.batch_id, v.phone_number, v.used_at, v.created_at, v.used FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? "
    params = [pid]
    if filter_type == 'used': query += " AND v.used=1"
    elif filter_type == 'unused': query += " AND v.used=0"
    query += " ORDER BY v.id DESC LIMIT 100"
    vouchers = db.execute(query, params).fetchall()
    rows = ''.join(f'<tr><td>{v["code"]}</td><td>{v["pn"]}</td><td>{v["batch_id"] or "-"}</td><td>{v["phone_number"] or "-"}</td><td>{v["used_at"] or "-"}</td><td>Never</td></tr>' for v in vouchers) or '<tr><td colspan="6">No vouchers found.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Vouchers <a href="/vouchers/bulk" class="btn btn-success btn-small">Create Voucher</a></div>
    <div class="tabs"><a href="/vouchers?filter=all" class="tab {'active' if filter_type=='all' else ''}">All</a><a href="/vouchers?filter=unused" class="tab {'active' if filter_type=='unused' else ''}">Unused</a><a href="/vouchers?filter=used" class="tab {'active' if filter_type=='used' else ''}">Used</a></div>
    <table><thead><tr><th>Voucher Code</th><th>Package</th><th>Batch</th><th>Used By</th><th>Used At</th><th>Unused Expiry</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Vouchers", content, get_pending_count(pid), pid, admin=True)

@app.route('/vouchers/bulk', methods=['GET','POST'])
@login_required
def vouchers_bulk():
    pid = session['provider_id']
    if request.method == 'POST':
        plan_id = int(request.form['plan_id']); count = int(request.form['count']); prefix = request.form.get('prefix','').strip().upper(); length = int(request.form['length']); expiry_days = request.form.get('expiry_days')
        batch_id = f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        db = get_db(); codes = []
        expiry_date = (date.today() + timedelta(days=int(expiry_days))).isoformat() if expiry_days else None
        for _ in range(count):
            if length > 0: code = prefix + ''.join(random.choices(string.ascii_uppercase+string.digits, k=length))
            else: code = generate_voucher_code()
            while db.execute("SELECT COUNT(*) as cnt FROM vouchers WHERE code=?",(code,)).fetchone()['cnt'] > 0:
                code = prefix + ''.join(random.choices(string.ascii_uppercase+string.digits, k=length)) if length > 0 else generate_voucher_code()
            db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, batch_id, expiry_date) VALUES (?,?,?,'bulk',?,?)",(pid, code, plan_id, batch_id, expiry_date)); codes.append(code)
        db.commit()
        content = f'<div class="card"><div class="alert alert-success">{count} vouchers generated!</div><p><strong>Batch ID:</strong> {batch_id}</p><div class="voucher-code" style="font-size:1rem;max-height:300px;overflow-y:auto;color:#fff;">{"<br>".join(codes)}</div><a href="/vouchers" class="btn">Back to Vouchers</a></div>'
        return render_page("Bulk Vouchers", content, get_pending_count(pid), pid, admin=True)
    content = f'''<div class="card"><div class="card-header">Generate Vouchers</div><form method="POST">
    <label>Package*</label><select name="plan_id" required>{get_plan_options(pid, public_only=False)}</select>
    <label>Number of Vouchers*</label><input type="number" name="count" value="10" min="1" required>
    <label>Voucher Prefix*</label><input type="text" name="prefix" placeholder="e.g. ROCK" maxlength="2">
    <label>Voucher Code Length* (6-12)</label><input type="number" name="length" value="8" min="6" max="12">
    <label>Unused Voucher Expiry (days, optional)</label><input type="number" name="expiry_days" placeholder="Leave empty for no expiry">
    <button type="submit" class="btn" style="margin-top:20px;">Generate</button></form></div>'''
    return render_page("Generate Vouchers", content, get_pending_count(pid), pid, admin=True)

# ------------------------------------------------------------
# INVOICES
# ------------------------------------------------------------
@app.route('/invoices')
@login_required
def invoices():
    pid = session['provider_id']; db = get_db(); items = db.execute("SELECT * FROM invoices WHERE provider_id=? ORDER BY id DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{i["invoice_no"]}</td><td>{i["user_id"]}</td><td>UGX {i["amount"] or 0:,}</td><td>UGX {i["paid_amount"] or 0:,}</td><td>UGX {(i["amount"] or 0)-(i["paid_amount"] or 0):,}</td><td>{i["status"]}</td><td>{i["due_date"]}</td></tr>' for i in items) or '<tr><td colspan="7">No invoices yet.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Invoices <a href="/invoices/add" class="btn btn-success btn-small">Create Invoice</a></div>
    <table><thead><tr><th>Invoice No.</th><th>User</th><th>Amount</th><th>Paid Amount</th><th>Remaining</th><th>Status</th><th>Due date</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Invoices", content, get_pending_count(pid), pid, admin=True)

@app.route('/invoices/add', methods=['GET','POST'])
@login_required
def add_invoice():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db(); inv_no = f"INV-{datetime.now().strftime('%Y%m')}-{random.randint(1000,9999)}"
        db.execute("INSERT INTO invoices (provider_id,invoice_no,user_id,amount,paid_amount,status,due_date) VALUES (?,?,?,?,0,'pending',?)",
                   (pid, inv_no, int(request.form['user_id']), float(request.form['amount']), request.form['due_date']))
        db.commit()
        return redirect('/invoices')
    return render_page("Create Invoice",'''<div class="card"><div class="card-header">Create Invoice</div>
    <form method="POST"><label>User ID*</label><input type="number" name="user_id" required><label>Amount (UGX)*</label><input type="number" name="amount" step="0.01" required><label>Due Date</label><input type="date" name="due_date"><button type="submit" class="btn" style="margin-top:20px;">Create</button></form></div>''', get_pending_count(pid), pid, admin=True)

# ------------------------------------------------------------
# EXPENSES
# ------------------------------------------------------------
@app.route('/expenses')
@login_required
def expenses():
    pid = session['provider_id']; db = get_db(); items = db.execute("SELECT * FROM expenses WHERE provider_id=? ORDER BY expense_date DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{e["expense_date"]}</td><td>{e["category"]}</td><td>UGX {e["amount"]:,.0f}</td><td>{e["payment_method"] or "-"}</td><td><a href="/expenses/edit/{e["id"]}" class="btn btn-small">Edit</a></td></tr>' for e in items) or '<tr><td colspan="5">No expenses yet.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Expenses <a href="/expenses/add" class="btn btn-success btn-small">Create Expense</a></div>
    <table><thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Method</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Expenses", content, get_pending_count(pid), pid, admin=True)

@app.route('/expenses/add', methods=['GET','POST'])
@login_required
def add_expense():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db(); db.execute("INSERT INTO expenses (provider_id,description,amount,category,expense_date,payment_method) VALUES (?,?,?,?,?,?)",
                   (pid, request.form['description'], float(request.form['amount']), request.form['type'], request.form['date'], request.form['method']))
        db.commit()
        return redirect('/expenses')
    return render_page("Create Expense",'''<div class="card"><div class="card-header">Create Expense</div>
    <form method="POST"><label>Type*</label><input type="text" name="type" required><label>Amount (UGX)*</label><input type="number" name="amount" step="0.01" required><label>Date*</label><input type="date" name="date" required><label>Payment method*</label><select name="method"><option value="cash">Cash</option><option value="mpesa">M-Pesa</option><option value="bank_transfer">Bank Transfer</option></select><label>Description</label><input type="text" name="description"><button type="submit" class="btn" style="margin-top:20px;">Create</button></form></div>''', get_pending_count(pid), pid, admin=True)

@app.route('/expenses/edit/<int:eid>', methods=['GET','POST'])
@login_required
def edit_expense(eid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE expenses SET description=?,amount=?,category=?,expense_date=?,payment_method=? WHERE id=? AND provider_id=?",(request.form['description'],float(request.form['amount']),request.form['type'],request.form['date'],request.form['method'],eid,session['provider_id'])); db.commit(); return redirect('/expenses')
    e = db.execute("SELECT * FROM expenses WHERE id=? AND provider_id=?",(eid,session['provider_id'])).fetchone()
    if not e: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Expense</div><form method="POST"><label>Type</label><input type="text" name="type" value="{e["category"]}" required><label>Amount (UGX)</label><input type="number" name="amount" step="0.01" value="{e["amount"]}" required><label>Date</label><input type="date" name="date" value="{e["expense_date"]}" required><label>Payment method</label><select name="method"><option value="cash" {"selected" if e["payment_method"]=="cash" else ""}>Cash</option><option value="mpesa" {"selected" if e["payment_method"]=="mpesa" else ""}>M-Pesa</option><option value="bank_transfer" {"selected" if e["payment_method"]=="bank_transfer" else ""}>Bank Transfer</option></select><label>Description</label><input type="text" name="description" value="{e["description"] or ""}"><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Expense", content, get_pending_count(pid), pid, admin=True)

@app.route('/expenses/delete/<int:eid>')
@login_required
def delete_expense(eid): db = get_db(); db.execute("DELETE FROM expenses WHERE id=? AND provider_id=?",(eid,session['provider_id'])); db.commit(); return redirect('/expenses')

# ------------------------------------------------------------
# MESSAGES
# ------------------------------------------------------------
@app.route('/messages', methods=['GET','POST'])
@login_required
def messages():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db(); db.execute("INSERT INTO sms_log (provider_id,phone_number,message) VALUES (?,?,?)",(pid,request.form.get('phone',''),request.form['message'])); db.commit()
        return redirect('/messages')
    db = get_db(); msgs = db.execute("SELECT * FROM sms_log WHERE provider_id=? ORDER BY id DESC LIMIT 50",(pid,)).fetchall()
    rows = "".join(f'<tr><td>{m["phone_number"]}</td><td>SMS</td><td>{m["message"]}</td><td>Yes</td><td>-</td><td>{m["sent_at"][:16] if m["sent_at"] else ""}</td></tr>' for m in msgs) or '<tr><td colspan="6">No messages sent.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Messages <a href="/messages/send" class="btn btn-success btn-small">Send message</a></div>
    <table><thead><tr><th>User</th><th>Channel</th><th>Message</th><th>Delivered</th><th>Cost</th><th>Sent</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Messages", content, get_pending_count(pid), pid, admin=True)

@app.route('/messages/send', methods=['GET','POST'])
@login_required
def send_message():
    pid = session['provider_id']
    if request.method == 'POST':
        return redirect('/messages')
    return render_page("Create Message",'''<div class="card"><div class="card-header">Create Message</div>
    <form method="POST"><label>Channel</label><select><option>SMS</option><option>WhatsApp</option></select><label>Message*</label><textarea name="message" required></textarea><button type="submit" class="btn" style="margin-top:20px;">Send</button></form></div>''', get_pending_count(pid), pid, admin=True)

# ------------------------------------------------------------
# EMAIL
# ------------------------------------------------------------
@app.route('/email', methods=['GET','POST'])
@login_required
def email():
    pid = session['provider_id']
    if request.method == 'POST':
        return redirect('/email')
    return render_page("Emails",'''<div class="card"><div class="card-header">Emails <a href="/email/send" class="btn btn-success btn-small">Send Email</a></div>
    <table><thead><tr><th>Subject</th><th>Email</th><th>Message</th><th>Status</th><th>Sent At</th></tr></thead><tbody><tr><td colspan="5">No emails sent yet.</td></tr></tbody></table></div>''', get_pending_count(pid), pid, admin=True)

@app.route('/email/send', methods=['GET','POST'])
@login_required
def send_email():
    pid = session['provider_id']
    if request.method == 'POST':
        return redirect('/email')
    return render_page("Send Email",'''<div class="card"><div class="card-header">Send Email</div>
    <form method="POST"><label>Subject*</label><input type="text" name="subject" required><label>Message*</label><textarea name="message" required></textarea><button type="submit" class="btn" style="margin-top:20px;">Send</button></form></div>''', get_pending_count(pid), pid, admin=True)

# ------------------------------------------------------------
# CAMPAIGNS
# ------------------------------------------------------------
@app.route('/campaign')
@login_required
def campaign():
    pid = session['provider_id']; db = get_db(); items = db.execute("SELECT * FROM campaigns WHERE provider_id=? ORDER BY id DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{c["name"]}</td><td>{c["kind"]}</td><td>{c["type"]}</td><td>{c["start_date"] or "-"}</td><td>{c["end_date"] or "-"}</td><td>{c["status"]}</td><td>0</td><td>0</td><td>0</td></tr>' for c in items) or '<tr><td colspan="9">No campaigns found.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Campaigns <a href="/campaign/add" class="btn btn-success btn-small">Create Campaign</a></div>
    <table><thead><tr><th>Campaign</th><th>Kind</th><th>Type</th><th>Scheduled At</th><th>End Date</th><th>Status</th><th>Targets</th><th>Sent</th><th>Failed</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Campaigns", content, get_pending_count(pid), pid, admin=True)

@app.route('/campaign/add', methods=['GET','POST'])
@login_required
def add_campaign():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db(); db.execute("INSERT INTO campaigns (provider_id,name,description,kind,type,start_date,end_date,status) VALUES (?,?,?,?,?,?,?,'active')",
                   (pid, request.form['name'], request.form['description'], request.form['kind'], request.form['type'], request.form['start_date'], request.form['end_date']))
        db.commit()
        return redirect('/campaign')
    return render_page("Create Campaign",'''<div class="card"><div class="card-header">Create Campaign</div>
    <form method="POST"><label>Name*</label><input type="text" name="name" required><label>Kind*</label><select name="kind"><option value="Portal Ad">Portal Ad</option><option value="Message">Message</option></select><label>Type*</label><input type="text" name="type" required><label>Start Date*</label><input type="date" name="start_date" required><label>End Date</label><input type="date" name="end_date"><label>Description</label><textarea name="description"></textarea><button type="submit" class="btn" style="margin-top:20px;">Create</button></form></div>''', get_pending_count(pid), pid, admin=True)

@app.route('/campaign/edit/<int:cid>', methods=['GET','POST'])
@login_required
def edit_campaign(cid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE campaigns SET name=?,description=?,kind=?,type=?,start_date=?,end_date=?,status=? WHERE id=? AND provider_id=?",(request.form['name'],request.form['description'],request.form['kind'],request.form['type'],request.form['start_date'],request.form['end_date'],request.form['status'],cid,session['provider_id'])); db.commit(); return redirect('/campaign')
    c = db.execute("SELECT * FROM campaigns WHERE id=? AND provider_id=?",(cid,session['provider_id'])).fetchone()
    if not c: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Campaign</div><form method="POST"><label>Name*</label><input type="text" name="name" value="{c["name"]}" required><label>Kind</label><select name="kind"><option value="Portal Ad" {"selected" if c["kind"]=="Portal Ad" else ""}>Portal Ad</option><option value="Message" {"selected" if c["kind"]=="Message" else ""}>Message</option></select><label>Type</label><input type="text" name="type" value="{c["type"] or ""}" required><label>Start Date</label><input type="date" name="start_date" value="{c["start_date"] if c["start_date"] else ""}" required><label>End Date</label><input type="date" name="end_date" value="{c["end_date"] if c["end_date"] else ""}"><label>Status</label><select name="status"><option value="active" {"selected" if c["status"]=="active" else ""}>Active</option><option value="inactive" {"selected" if c["status"]=="inactive" else ""}>Inactive</option></select><label>Description</label><textarea name="description">{c["description"] or ""}</textarea><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Campaign", content, get_pending_count(pid), pid, admin=True)

@app.route('/campaign/delete/<int:cid>')
@login_required
def delete_campaign(cid): db = get_db(); db.execute("DELETE FROM campaigns WHERE id=? AND provider_id=?",(cid,session['provider_id'])); db.commit(); return redirect('/campaign')

# ------------------------------------------------------------
# MIKROTIK
# ------------------------------------------------------------
@app.route('/mikrotik')
@login_required
def mikrotik():
    pid = session['provider_id']; db = get_db(); routers = db.execute("SELECT * FROM mikrotik_routers WHERE provider_id=? ORDER BY id DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{r["name"]}</td><td>{r["ip_address"] or ""}</td><td>{"Online" if r["is_active"] else "Offline"}</td><td>-</td><td>{"Active" if r["is_active"] else "Inactive"}</td><td><a href="/mikrotik/edit/{r["id"]}" class="btn btn-small">Edit</a> <a href="/mikrotik/delete/{r["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for r in routers) or '<tr><td colspan="6">No Nas devices.</td></tr>'
    content = f'''<div class="card"><div class="card-header">MikroTik Routers <a href="/mikrotik/add" class="btn btn-success btn-small">Link a MikroTik</a></div>
    <table><thead><tr><th>Board Name</th><th>Provisioning</th><th>CPU</th><th>Memory</th><th>Status</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("MikroTik", content, get_pending_count(pid), pid, admin=True)

@app.route('/mikrotik/add', methods=['GET','POST'])
@login_required
def add_mikrotik():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db(); db.execute("INSERT INTO mikrotik_routers (provider_id,name,ip_address,username,password,api_port,is_active) VALUES (?,?,?,?,?,?,?)",
                   (pid, request.form['name'], request.form['ip'], request.form['username'], request.form['password'], int(request.form.get('port', 8728)), 1 if request.form.get('is_active') else 0))
        db.commit()
        return redirect('/mikrotik')
    return render_page("Add MikroTik Device",'''<div class="card"><div class="card-header">Add MikroTik Device</div>
    <form method="POST"><label>Mikrotik Identity*</label><input type="text" name="name" required><label>IP Address</label><input type="text" name="ip"><label>Username</label><input type="text" name="username"><label>Password</label><input type="password" name="password"><label>API Port</label><input type="number" name="port" value="8728"><label><input type="checkbox" name="is_active" checked> Active</label><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>''', get_pending_count(pid), pid, admin=True)

@app.route('/mikrotik/edit/<int:rid>', methods=['GET','POST'])
@login_required
def edit_mikrotik(rid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE mikrotik_routers SET name=?,ip_address=?,username=?,password=?,api_port=?,is_active=? WHERE id=? AND provider_id=?",(request.form['name'],request.form['ip'],request.form['username'],request.form['password'],int(request.form['port'] or 8728),1 if request.form.get('is_active') else 0,rid,session['provider_id'])); db.commit(); return redirect('/mikrotik')
    r = db.execute("SELECT * FROM mikrotik_routers WHERE id=? AND provider_id=?",(rid,session['provider_id'])).fetchone()
    if not r: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit MikroTik Router</div><form method="POST"><label>Name*</label><input type="text" name="name" value="{r["name"]}" required><label>IP Address</label><input type="text" name="ip" value="{r["ip_address"] or ""}"><label>Username</label><input type="text" name="username" value="{r["username"] or ""}"><label>Password</label><input type="password" name="password" value="{r["password"] or ""}"><label>API Port</label><input type="number" name="port" value="{r["api_port"] or 8728}"><label><input type="checkbox" name="is_active" {"checked" if r["is_active"] else ""}> Active</label><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit MikroTik", content, get_pending_count(pid), pid, admin=True)

@app.route('/mikrotik/delete/<int:rid>')
@login_required
def delete_mikrotik(rid): db = get_db(); db.execute("DELETE FROM mikrotik_routers WHERE id=? AND provider_id=?",(rid,session['provider_id'])); db.commit(); return redirect('/mikrotik')

# ------------------------------------------------------------
# EQUIPMENT
# ------------------------------------------------------------
@app.route('/equipment')
@login_required
def equipment():
    pid = session['provider_id']; db = get_db(); items = db.execute("SELECT * FROM equipment WHERE provider_id=? ORDER BY id DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{e["user_id"] or "-"}</td><td>{e["model"] or "-"}</td><td>{e["name"]}</td><td>UGX {e["price"] or 0:,}</td><td>UGX {e["paid_amount"] or 0:,}</td><td><a href="/equipment/edit/{e["id"]}" class="btn btn-small">Edit</a></td></tr>' for e in items) or '<tr><td colspan="6">No equipment yet.</td></tr>'
    content = f'''<div class="card"><div class="card-header">Equipment <a href="/equipment/add" class="btn btn-success btn-small">Add Equipment</a></div>
    <table><thead><tr><th>User</th><th>Type</th><th>Equipment Name</th><th>Equipment Price</th><th>Paid Amount</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table></div>'''
    return render_page("Equipment", content, get_pending_count(pid), pid, admin=True)

@app.route('/equipment/add', methods=['GET','POST'])
@login_required
def add_equipment():
    pid = session['provider_id']
    if request.method == 'POST':
        db = get_db(); db.execute("INSERT INTO equipment (provider_id,name,model,serial_number,user_id,price,paid_amount) VALUES (?,?,?,?,?,?,?)",
                   (pid, request.form['name'], request.form['model'], request.form['serial'], int(request.form.get('user_id', 0)), float(request.form.get('price', 0)), float(request.form.get('paid', 0))))
        db.commit()
        return redirect('/equipment')
    return render_page("Create Equipment",'''<div class="card"><div class="card-header">Create Equipment</div>
    <form method="POST"><label>Name*</label><input type="text" name="name" required><label>Model</label><input type="text" name="model"><label>Serial Number</label><input type="text" name="serial"><label>Price (UGX)</label><input type="number" name="price" step="0.01"><label>Paid Amount (UGX)</label><input type="number" name="paid" step="0.01"><button type="submit" class="btn" style="margin-top:20px;">Create</button></form></div>''', get_pending_count(pid), pid, admin=True)

@app.route('/equipment/edit/<int:eid>', methods=['GET','POST'])
@login_required
def edit_equipment(eid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE equipment SET name=?,model=?,serial_number=?,price=?,paid_amount=? WHERE id=? AND provider_id=?",(request.form['name'],request.form['model'],request.form['serial'],float(request.form.get('price',0)),float(request.form.get('paid',0)),eid,session['provider_id'])); db.commit(); return redirect('/equipment')
    eq = db.execute("SELECT * FROM equipment WHERE id=? AND provider_id=?",(eid,session['provider_id'])).fetchone()
    if not eq: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Equipment</div><form method="POST"><label>Name*</label><input type="text" name="name" value="{eq["name"]}" required><label>Model</label><input type="text" name="model" value="{eq["model"] or ""}"><label>Serial Number</label><input type="text" name="serial" value="{eq["serial_number"] or ""}"><label>Price (UGX)</label><input type="number" name="price" step="0.01" value="{eq["price"] or 0}"><label>Paid Amount (UGX)</label><input type="number" name="paid" step="0.01" value="{eq["paid_amount"] or 0}"><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Equipment", content, get_pending_count(pid), pid, admin=True)

@app.route('/equipment/delete/<int:eid>')
@login_required
def delete_equipment(eid): db = get_db(); db.execute("DELETE FROM equipment WHERE id=? AND provider_id=?",(eid,session['provider_id'])); db.commit(); return redirect('/equipment')

# ------------------------------------------------------------
# PROVIDER SETTINGS
# ------------------------------------------------------------
@app.route('/provider/edit')
@login_required
def provider_edit_redirect():
    # Redirect to the new unified settings page, defaulting to the "General" tab
    return redirect(url_for('settings', tab='general'))

@app.route('/toggle-auto')
@login_required
def toggle_auto():
    db = get_db(); cur = get_auto_approve(session['provider_id']); db.execute("UPDATE providers SET auto_approve=? WHERE id=?",(0 if cur else 1, session['provider_id'])); db.commit()
    return redirect('/dashboard')

@app.route('/stats')
@login_required
def stats():
    pid = session['provider_id']; db = get_db(); today = date.today().isoformat()
    sms = db.execute("SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at)=?",(pid,today)).fetchone()
    cash = db.execute("SELECT COUNT(*) as c, COALESCE(SUM(pl.price_ugx),0) as t FROM vouchers v JOIN plans pl ON v.plan_id=pl.id WHERE v.provider_id=? AND v.payment_method='cash' AND date(v.created_at)=?",(pid,today)).fetchone()
    used = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=? AND used=1",(pid,)).fetchone()['c']
    unused = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=? AND used=0",(pid,)).fetchone()['c']
    pstats = db.execute("SELECT p.name, COUNT(*) as c FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? GROUP BY p.name ORDER BY c DESC",(pid,)).fetchall()
    wf, ws, we = get_weekly_platform_revenue(pid)
    content = f"""<div class="stat-grid"><div class="card" style="text-align:center;"><h3>UGX {sms['t'] or 0:,}</h3><small>SMS Revenue Today</small></div><div class="card" style="text-align:center;"><h3>UGX {cash['t'] or 0:,}</h3><small>Cash Revenue Today</small></div><div class="card" style="text-align:center;"><h3>{used}</h3><small>Vouchers Used</small></div><div class="card" style="text-align:center;"><h3>{unused}</h3><small>Vouchers Unused</small></div><div class="card" style="text-align:center;"><h3>{get_pending_count(pid)}</h3><small>Pending</small></div></div>
    <div class="platform-revenue"><strong>RockabyTech Platform Fee (5% this week):</strong> UGX {wf:,} &nbsp; <small>({ws.strftime('%d %b')} - {we.strftime('%d %b')})</small></div>
    <div class="card"><div class="card-header">Top Selling Plans</div><table><tr><th>Plan</th><th>Sold</th></tr>{''.join(f'<tr><td>{p["name"]}</td><td>{p["c"]}</td></tr>' for p in pstats) or '<tr><td colspan="2">No sales yet.</td></tr>'}</table></div><a href="/dashboard" class="btn btn-outline">Back to Dashboard</a>"""
    return render_page("Statistics", content, get_pending_count(pid), pid, admin=True)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    pid = session['provider_id']
    db = get_db()
    provider = get_provider(pid)
    tab = request.args.get('tab', 'general')

    # ========== HANDLE POST ==========
    if request.method == 'POST':
        tab = request.form.get('tab', 'general')

        if tab == 'general':
            # Update business name, support phone, poster, logo
            business_name = request.form['business_name']
            support_phone = request.form.get('support_phone', '')
            poster = request.files.get('poster')
            logo = request.files.get('logo')

            poster_filename = provider['poster_image'] if provider else None
            logo_filename = provider['logo_image'] if provider else None

            if poster and poster.filename and allowed_file(poster.filename):
                poster_filename = secure_filename(poster.filename)
                poster.save(os.path.join(app.config['UPLOAD_FOLDER'], poster_filename))
            if logo and logo.filename and allowed_file(logo.filename):
                logo_filename = secure_filename(logo.filename)
                logo.save(os.path.join(app.config['UPLOAD_FOLDER'], logo_filename))

            db.execute("""
                UPDATE providers 
                SET business_name=?, support_phone=?, poster_image=?, logo_image=? 
                WHERE id=?
            """, (business_name, support_phone, poster_filename, logo_filename, pid))
            db.commit()
            session['provider_name'] = business_name

            # Save the selected theme
            theme = request.form.get('captive_portal_theme', 'default')
            set_setting(pid, 'captive_portal_theme', theme)

        elif tab == 'payments':
            # Save MTN/Airtel and Yo! Payments
            mtn = request.form.get('mtn_number', '')
            airtel = request.form.get('airtel_number', '')
            yo_user = request.form.get('yo_username', '')
            yo_pass = request.form.get('yo_password', '')
            yo_auto = 1 if request.form.get('yo_auto_pay') else 0

            # Save active payment method
            active_method = request.form.get('active_payment_method', 'manual')
            set_setting(pid, 'active_payment_method', active_method)

            # ===== IOTEC Credentials =====
            set_setting(pid, 'iotec_wallet_id', request.form.get('iotec_wallet_id', ''))
            set_setting(pid, 'iotec_client_id', request.form.get('iotec_client_id', ''))
            set_setting(pid, 'iotec_api_secret', request.form.get('iotec_api_secret', ''))

            # ===== PawaPay Credentials =====
            set_setting(pid, 'pawapay_api_key', request.form.get('pawapay_api_key', ''))
            set_setting(pid, 'pawapay_merchant_id', request.form.get('pawapay_merchant_id', ''))

            # ===== PesaPal Credentials =====
            set_setting(pid, 'pesapal_consumer_key', request.form.get('pesapal_consumer_key', ''))
            set_setting(pid, 'pesapal_consumer_secret', request.form.get('pesapal_consumer_secret', ''))

            db.execute("""
                UPDATE providers 
                SET mtn_number=?, airtel_number=?, yo_username=?, yo_password=?, yo_auto_pay=? 
                WHERE id=?
            """, (mtn, airtel, yo_user, yo_pass, yo_auto, pid))
            db.commit()

        elif tab == 'pppoe':
            set_setting(pid, 'pppoe_radius_server', request.form.get('radius_server', ''))
            set_setting(pid, 'pppoe_nas_ip', request.form.get('nas_ip', ''))
            set_setting(pid, 'pppoe_secret', request.form.get('secret', ''))

        elif tab == 'hotspot':
            set_setting(pid, 'hotspot_session_timeout', request.form.get('session_timeout', '3600'))
            set_setting(pid, 'hotspot_idle_timeout', request.form.get('idle_timeout', '300'))

        elif tab == 'sms':
            set_setting(pid, 'sms_gateway', request.form.get('gateway', ''))
            set_setting(pid, 'sms_api_key', request.form.get('api_key', ''))

        elif tab == 'whatsapp':
            set_setting(pid, 'whatsapp_phone_number_id', request.form.get('phone_number_id', ''))
            set_setting(pid, 'whatsapp_access_token', request.form.get('access_token', ''))

        elif tab == 'notifications':
            set_setting(pid, 'notify_email', request.form.get('notify_email', ''))
            set_setting(pid, 'notify_sms', request.form.get('notify_sms', ''))

        return redirect(url_for('settings', tab=tab))

    # ========== HANDLE GET ==========
    # Load current values for each tab
    current_theme = get_setting(pid, 'captive_portal_theme', 'default')
    active_payment_method = get_setting(pid, 'active_payment_method', 'manual')

    # Load credentials for all payment methods
    iotec_wallet_id = get_setting(pid, 'iotec_wallet_id', '')
    iotec_client_id = get_setting(pid, 'iotec_client_id', '')
    iotec_api_secret = get_setting(pid, 'iotec_api_secret', '')
    pawapay_api_key = get_setting(pid, 'pawapay_api_key', '')
    pawapay_merchant_id = get_setting(pid, 'pawapay_merchant_id', '')
    pesapal_consumer_key = get_setting(pid, 'pesapal_consumer_key', '')
    pesapal_consumer_secret = get_setting(pid, 'pesapal_consumer_secret', '')

    pppoe_data = {
        'radius_server': get_setting(pid, 'pppoe_radius_server', ''),
        'nas_ip': get_setting(pid, 'pppoe_nas_ip', ''),
        'secret': get_setting(pid, 'pppoe_secret', ''),
    }
    hotspot_data = {
        'session_timeout': get_setting(pid, 'hotspot_session_timeout', '3600'),
        'idle_timeout': get_setting(pid, 'hotspot_idle_timeout', '300'),
    }
    sms_data = {
        'gateway': get_setting(pid, 'sms_gateway', ''),
        'api_key': get_setting(pid, 'sms_api_key', ''),
    }
    whatsapp_data = {
        'phone_number_id': get_setting(pid, 'whatsapp_phone_number_id', ''),
        'access_token': get_setting(pid, 'whatsapp_access_token', ''),
    }
    notifications_data = {
        'notify_email': get_setting(pid, 'notify_email', ''),
        'notify_sms': get_setting(pid, 'notify_sms', ''),
    }

    # Build tab navigation
    tabs = [
        ('general', 'General'),
        ('payments', 'Payments'),
        ('pppoe', 'PPPoE'),
        ('hotspot', 'Hotspot'),
        ('sms', 'SMS'),
        ('whatsapp', 'WhatsApp'),
        ('notifications', 'Notifications'),
    ]
    nav = ''.join(f'<a href="/settings?tab={t[0]}" class="tab {"active" if tab==t[0] else ""}">{t[1]}</a>' for t in tabs)

    # ========== BUILD CONTENT PER TAB ==========
    if tab == 'general':
        poster_preview = f'<p>Current poster: <img src="/static/uploads/{provider["poster_image"]}" style="max-width:200px;border-radius:8px;"></p>' if provider and provider['poster_image'] else ''
        logo_preview = f'<p>Current logo: <img src="/static/uploads/{provider["logo_image"]}" style="max-width:100px;border-radius:8px;"></p>' if provider and provider['logo_image'] else ''

        theme_options = f'''
        <select name="captive_portal_theme" style="width:auto; min-width:200px;">
            <option value="default" {"selected" if current_theme == 'default' else ""}>Default – Original Design</option>
            <option value="neon" {"selected" if current_theme == 'neon' else ""}>Neon – Vibrant & Glowing</option>
            <option value="minimalist" {"selected" if current_theme == 'minimalist' else ""}>Minimalist – Clean & Simple</option>
        </select>
        '''

        content = f'''
        <div class="card">
            <div class="card-header">General Settings</div>
            <form method="POST" enctype="multipart/form-data">
                <input type="hidden" name="tab" value="general">
                <label>Business Name</label>
                <input type="text" name="business_name" value="{provider["business_name"] if provider else ""}" required>
                <label>Support WhatsApp</label>
                <input type="text" name="support_phone" value="{provider["support_phone"] if provider else ""}">
                <label>Portal Poster/Banner</label>
                <input type="file" name="poster" accept="image/*">
                {poster_preview}
                <label>Business Logo</label>
                <input type="file" name="logo" accept="image/*">
                {logo_preview}
                <label>Captive Portal Theme</label>
                {theme_options}
                <small style="display:block; color:var(--text-secondary); margin-top:5px;">Choose the look of your WiFi login page.</small>
                <button type="submit" class="btn" style="margin-top:20px;">Save General Settings</button>
            </form>
        </div>
        '''

        elif tab == 'payments':
        # Build payment method options
        payment_methods = [
            ('manual', 'Manual (SMS Verification)'),
            ('yo', 'Yo! Payments'),
            ('iotec', 'IOTEC'),
            ('pawapay', 'PawaPay'),
            ('pesapal', 'PesaPal'),
        ]
        method_options = ''.join(
            f'<option value="{val}" {"selected" if active_payment_method == val else ""}>{label}</option>'
            for val, label in payment_methods
        )

        # Load the payment name from settings
        payment_name = get_setting(pid, 'payment_name', '')

        # Generate callback URLs
        iotec_callback_url = url_for('iotec_callback', _external=True)
        pawapay_callback_url = url_for('pawapay_callback', _external=True)
        pesapal_callback_url = url_for('pesapal_callback', _external=True)

        content = f'''
        <div class="card">
            <div class="card-header">Payment Settings</div>
            <form method="POST">
                <input type="hidden" name="tab" value="payments">

                <h4>MTN / Airtel (for Manual SMS)</h4>
                <label>MTN Mobile Money Number</label>
                <input type="text" name="mtn_number" value="{provider["mtn_number"] if provider else ''}">
                <label>Airtel Money Number</label>
                <input type="text" name="airtel_number" value="{provider["airtel_number"] if provider else ''}">

                <!-- NEW FIELD: Payment Name -->
                <label>Payment Name (displayed to customers)</label>
                <input type="text" name="payment_name" value="{payment_name}">
                <small style="color:var(--text-secondary);">This name will appear on the payment instructions page.</small>

                <hr>
                <h4>Yo! Payments</h4>
                <label>Username</label>
                <input type="text" name="yo_username" value="{provider["yo_username"] if provider else ''}">
                <label>Password</label>
                <input type="password" name="yo_password" value="{provider["yo_password"] if provider else ''}">
                <label>
                    <input type="checkbox" name="yo_auto_pay" {"checked" if provider and provider["yo_auto_pay"] else ""}>
                    Enable Yo! Auto‑Pay
                </label>

                <hr>
                <h4>IOTEC</h4>
                <label>Wallet ID</label>
                <input type="text" name="iotec_wallet_id" value="{iotec_wallet_id}">
                <label>Client ID</label>
                <input type="text" name="iotec_client_id" value="{iotec_client_id}">
                <label>API Secret</label>
                <input type="password" name="iotec_api_secret" value="{iotec_api_secret}">
                <label>Callback URL</label>
                <input type="text" readonly value="{iotec_callback_url}" style="background:var(--bg);">
                <small style="display:block; color:var(--text-secondary);">Copy this URL and paste it in your IOTEC configuration.</small>

                <hr>
                <h4>PawaPay</h4>
                <label>API Key</label>
                <input type="text" name="pawapay_api_key" value="{pawapay_api_key}">
                <label>Merchant ID</label>
                <input type="text" name="pawapay_merchant_id" value="{pawapay_merchant_id}">
                <label>Callback URL</label>
                <input type="text" readonly value="{pawapay_callback_url}" style="background:var(--bg);">
                <small style="display:block; color:var(--text-secondary);">Copy this URL and paste it in your PawaPay configuration.</small>

                <hr>
                <h4>PesaPal</h4>
                <label>Consumer Key</label>
                <input type="text" name="pesapal_consumer_key" value="{pesapal_consumer_key}">
                <label>Consumer Secret</label>
                <input type="password" name="pesapal_consumer_secret" value="{pesapal_consumer_secret}">
                <label>Callback URL</label>
                <input type="text" readonly value="{pesapal_callback_url}" style="background:var(--bg);">
                <small style="display:block; color:var(--text-secondary);">Copy this URL and paste it in your PesaPal configuration.</small>

                <hr>
                <label>Default Payment Method (used on the captive portal)</label>
                <select name="active_payment_method" style="width:auto; min-width:200px;">
                    {method_options}
                </select>
                <small style="display:block; color:var(--text-secondary); margin-top:5px;">Choose which payment method users will see when buying internet.</small>

                <button type="submit" class="btn" style="margin-top:20px;">Save Payment Settings</button>
            </form>
        </div>
        '''

    elif tab == 'pppoe':
        content = f'''
        <div class="card">
            <div class="card-header">PPPoE Settings</div>
            <form method="POST">
                <input type="hidden" name="tab" value="pppoe">
                <label>RADIUS Server</label>
                <input type="text" name="radius_server" value="{pppoe_data['radius_server']}">
                <label>NAS IP Address</label>
                <input type="text" name="nas_ip" value="{pppoe_data['nas_ip']}">
                <label>RADIUS Secret</label>
                <input type="password" name="secret" value="{pppoe_data['secret']}">
                <button type="submit" class="btn" style="margin-top:20px;">Save PPPoE Settings</button>
            </form>
        </div>
        '''

    elif tab == 'hotspot':
        content = f'''
        <div class="card">
            <div class="card-header">Hotspot Settings</div>
            <form method="POST">
                <input type="hidden" name="tab" value="hotspot">
                <label>Session Timeout (seconds)</label>
                <input type="number" name="session_timeout" value="{hotspot_data['session_timeout']}">
                <label>Idle Timeout (seconds)</label>
                <input type="number" name="idle_timeout" value="{hotspot_data['idle_timeout']}">
                <button type="submit" class="btn" style="margin-top:20px;">Save Hotspot Settings</button>
            </form>
        </div>
        '''

    elif tab == 'sms':
        content = f'''
        <div class="card">
            <div class="card-header">SMS Gateway Settings</div>
            <form method="POST">
                <input type="hidden" name="tab" value="sms">
                <label>Gateway (e.g., Twilio, AfricasTalking)</label>
                <input type="text" name="gateway" value="{sms_data['gateway']}">
                <label>API Key / Token</label>
                <input type="password" name="api_key" value="{sms_data['api_key']}">
                <button type="submit" class="btn" style="margin-top:20px;">Save SMS Settings</button>
            </form>
        </div>
        '''

    elif tab == 'whatsapp':
        content = f'''
        <div class="card">
            <div class="card-header">WhatsApp Business API</div>
            <form method="POST">
                <input type="hidden" name="tab" value="whatsapp">
                <label>Phone Number ID</label>
                <input type="text" name="phone_number_id" value="{whatsapp_data['phone_number_id']}">
                <label>Access Token</label>
                <input type="password" name="access_token" value="{whatsapp_data['access_token']}">
                <button type="submit" class="btn" style="margin-top:20px;">Save WhatsApp Settings</button>
            </form>
        </div>
        '''

    elif tab == 'notifications':
        content = f'''
        <div class="card">
            <div class="card-header">Notification Preferences</div>
            <form method="POST">
                <input type="hidden" name="tab" value="notifications">
                <label>Email for notifications</label>
                <input type="email" name="notify_email" value="{notifications_data['notify_email']}">
                <label>SMS for notifications (phone)</label>
                <input type="text" name="notify_sms" value="{notifications_data['notify_sms']}">
                <button type="submit" class="btn" style="margin-top:20px;">Save Notification Settings</button>
            </form>
        </div>
        '''

    else:
        content = '<div class="card"><p>Select a tab to configure settings.</p></div>'

    # Wrap with tabs navigation
    full_content = f'''
    <div class="tabs" style="margin-bottom:20px;">{nav}</div>
    {content}
    '''

    return render_page("Settings", full_content, get_pending_count(pid), pid, admin=True)

@app.route('/system-users')
@login_required
def system_users():
    return render_page("System Users", '<div class="card"><p>Manage admin users and permissions (coming soon).</p></div>', get_pending_count(session['provider_id']), session['provider_id'], admin=True)

@app.route('/system-logs')
@login_required
def system_logs():
    return render_page("System Logs", '<div class="card"><p>Audit logs and activity history will appear here.</p></div>', get_pending_count(session['provider_id']), session['provider_id'], admin=True)

@app.route('/refer')
@login_required
def refer():
    return render_page("Refer a Friend", '<div class="card"><p>Your referral link: <code>https://rockabywifi.com/?ref=...</code></p></div>', get_pending_count(session['provider_id']), session['provider_id'], admin=True)

@app.route('/docs')
@login_required
def docs():
    return render_page("Documentation", '<div class="card"><p>Documentation and help resources will be linked here.</p></div>', get_pending_count(session['provider_id']), session['provider_id'], admin=True)

@app.route('/billing')
@login_required
def billing():
    pid = session['provider_id']
    db = get_db()
    provider = get_provider(pid)
    
    # Subscription Expiry
    subscription_expiry = provider['subscription_expiry'] if provider and provider['subscription_expiry'] else None
    is_expired = False
    days_until_expiry = 0
    
    if subscription_expiry:
        expiry_date = datetime.strptime(subscription_expiry, '%Y-%m-%d')
        today = datetime.now().date()
        days_until_expiry = (expiry_date.date() - today).days
        is_expired = days_until_expiry < 0
    
    # Calculate platform fee (use provider's percent_fee)
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    total_revenue = db.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM voucher_requests "
        "WHERE provider_id = ? AND status = 'approved' AND date(created_at) >= ?",
        (pid, month_start)
    ).fetchone()['total']
    
    # FIXED: Use bracket access with is not None checks
    percent = provider['percent_fee'] if provider and provider['percent_fee'] is not None else 5.0
    platform_fee = int(total_revenue * (percent / 100))
    
    # FIXED: Use bracket access with is not None checks
    monthly_fee = provider['monthly_fee_ugx'] if provider and provider['monthly_fee_ugx'] is not None else 20000
    total_due = platform_fee + monthly_fee
    
    # Generate dynamic invoice number
    current_year_month = datetime.now().strftime('%Y%m')
    count_invoices = db.execute(
        "SELECT COUNT(*) as cnt FROM invoices "
        "WHERE provider_id = ? AND invoice_no LIKE ?",
        (pid, f'INV-{current_year_month}-%')
    ).fetchone()['cnt']
    next_sequence = count_invoices + 1
    invoice_number = f"INV-{current_year_month}-{next_sequence:03d}"
    
    # Payment history
    payments = db.execute(
        "SELECT amount, created_at, status, transaction_id, voucher_code FROM voucher_requests "
        "WHERE provider_id = ? AND phone_number = 'manual' AND status = 'approved' "
        "ORDER BY created_at DESC LIMIT 10",
        (pid,)
    ).fetchall()
    
    payment_rows = ''
    if payments:
        for p in payments:
            payment_rows += f'''
            <tr>
                <td>UGX {p['amount']:,}</td>
                <td>{p['created_at'][:16] if p['created_at'] else '-'}</td>
                <td>{p['status']}</td>
                <td>{p['voucher_code'] or '-'}</td>
            </tr>
            '''
    else:
        payment_rows = '''
        <tr>
            <td colspan="4" style="text-align:center; padding:30px; color:var(--text-secondary);">
                No payments found. You have not made any payments through the system yet.
            </td>
        </tr>
        '''
    
    # Format expiry date
    if subscription_expiry:
        expiry_display = datetime.strptime(subscription_expiry, '%Y-%m-%d').strftime('%d.%m.%Y')
        expiry_display += " at 07:24 PM"
    else:
        expiry_display = "Not set"
    
    # Check if payment is available (within 5 days of expiry)
    can_pay = days_until_expiry <= 5 and days_until_expiry >= 0
    payment_available_message = ''
    if can_pay:
        payment_available_message = f'''
        <div style="background:rgba(40,167,69,0.1); border-left:4px solid #28a745; padding:20px; border-radius:4px; margin:20px 0;">
            <h4 style="color:#28a745; margin:0 0 10px 0;">✅ Payment Available</h4>
            <p style="margin:0; color:var(--text-secondary);">
                Your subscription expires in {days_until_expiry} day(s). You can now renew your license.
            </p>
            <a href="#" class="btn" style="margin-top:10px;">Pay Now UGX {total_due:,}</a>
        </div>
        '''
    else:
        payment_available_message = f'''
        <div style="background:rgba(255,193,7,0.1); border-left:4px solid #ffc107; padding:20px; border-radius:4px; margin:20px 0;">
            <h4 style="color:#ffc107; margin:0 0 10px 0;">Payment Not Available</h4>
            <p style="margin:0; color:var(--text-secondary);">
                License renewal payments can only be made within 5 days of your subscription expiry date. 
                Please check back in a few days to make a payment.
            </p>
        </div>
        '''
    
    # Invoice Modal
    invoice_modal = f'''
    <div id="invoiceModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.7); z-index:99999; overflow-y:auto; padding:40px 20px;">
        <div style="max-width:800px; margin:0 auto; background:var(--card-bg); border-radius:var(--radius); padding:40px; box-shadow:var(--shadow); border:1px solid var(--glass-border); position:relative;">
            <button onclick="closeInvoiceModal()" style="position:absolute; top:15px; right:20px; background:none; border:none; font-size:1.8rem; cursor:pointer; color:var(--text);">&times;</button>
            
            <div style="display:flex; justify-content:space-between; margin-bottom:30px;">
                <div>
                    <h2 style="font-size:1.8rem; margin:0; color:var(--primary);">INVOICE</h2>
                    <p style="color:var(--text-secondary); margin:5px 0;">Invoice #{invoice_number}</p>
                    <p style="color:var(--text-secondary); margin:5px 0;">Date: {datetime.now().strftime('%d %B %Y')}</p>
                </div>
                <div style="text-align:right;">
                    <p style="font-weight:600; margin:0;">RockabyTech</p>
                    <p style="color:var(--text-secondary); margin:2px 0;">support@rockabytech.com</p>
                    <p style="color:var(--text-secondary); margin:0;">https://rockabytech.github.io/</p>
                </div>
            </div>
            
            <div style="margin-bottom:30px; padding:20px; background:var(--bg); border-radius:8px;">
                <h3 style="margin:0 0 10px 0; font-size:1rem; color:var(--text-secondary);">Bill To</h3>
                <p style="font-size:1.1rem; margin:0;"><strong>{provider["business_name"] if provider else "N/A"}</strong></p>
                <p style="margin:2px 0; color:var(--text-secondary);">Phone: {provider["contact"] if provider else "N/A"}</p>
                <p style="margin:2px 0; color:var(--text-secondary);">Email: {provider["support_phone"] if provider else "N/A"}</p>
            </div>
            
            <div style="margin-bottom:30px;">
                <p><strong>Status:</strong> <span style="color:#ffc107;">Pending</span></p>
            </div>
            
            <table style="width:100%; border-collapse:collapse; margin-bottom:20px;">
                <thead>
                    <tr style="background:var(--bg);">
                        <th style="padding:12px; text-align:left; border-bottom:2px solid var(--border);">Description</th>
                        <th style="padding:12px; text-align:right; border-bottom:2px solid var(--border);">Price</th>
                        <th style="padding:12px; text-align:center; border-bottom:2px solid var(--border);">Quantity</th>
                        <th style="padding:12px; text-align:right; border-bottom:2px solid var(--border);">Total</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td style="padding:12px; border-bottom:1px solid var(--border);">Platform Fee ({percent}% of monthly revenue)</td>
                        <td style="padding:12px; text-align:right; border-bottom:1px solid var(--border);">UGX {platform_fee:,}</td>
                        <td style="padding:12px; text-align:center; border-bottom:1px solid var(--border);">1</td>
                        <td style="padding:12px; text-align:right; border-bottom:1px solid var(--border);">UGX {platform_fee:,}</td>
                    </tr>
                    <tr>
                        <td style="padding:12px; border-bottom:1px solid var(--border);">Monthly Maintenance Fee</td>
                        <td style="padding:12px; text-align:right; border-bottom:1px solid var(--border);">UGX {monthly_fee:,}</td>
                        <td style="padding:12px; text-align:center; border-bottom:1px solid var(--border);">1</td>
                        <td style="padding:12px; text-align:right; border-bottom:1px solid var(--border);">UGX {monthly_fee:,}</td>
                    </tr>
                </tbody>
                <tfoot>
                    <tr>
                        <td colspan="3" style="padding:12px; text-align:right; font-weight:600; border-top:2px solid var(--border);">Service Subtotal:</td>
                        <td style="padding:12px; text-align:right; font-weight:600; border-top:2px solid var(--border);">UGX {total_due:,}</td>
                    </tr>
                    <tr>
                        <td colspan="3" style="padding:12px; text-align:right; font-weight:700; font-size:1.1rem; border-top:2px solid var(--border);">Total Due:</td>
                        <td style="padding:12px; text-align:right; font-weight:700; font-size:1.1rem; border-top:2px solid var(--border);">UGX {total_due:,}</td>
                    </tr>
                </tfoot>
            </table>
            
            {payment_available_message}
            
            <div style="text-align:center; padding:20px 0; border-top:1px solid var(--border); margin-top:20px;">
                <p style="color:var(--text-secondary); margin:5px 0;">Thank you for your business!</p>
                <p style="color:var(--text-secondary); margin:5px 0;">
                    For billing inquiries, please contact 
                    <a href="mailto:sales@rockabytech.com" style="color:var(--primary);">sales@rockabytech.com</a>
                </p>
                <p style="color:var(--text-secondary); margin:5px 0;">
                    Access your account and manage your services by logging in at:
                    <a href="{url_for('login', _external=True)}" style="color:var(--primary);">{url_for('login', _external=True)}</a>
                </p>
                <p style="color:var(--text-secondary); margin:10px 0 0 0; font-size:0.8rem;">
                    &copy; 2026 RockabyTech. All rights reserved.
                </p>
            </div>
        </div>
    </div>
    '''
    
    # Main content
    content = f'''
    <div class="card">
        <div class="card-header">
            <i class="fas fa-credit-card" style="color:var(--primary);"></i> 
            RockabyWiFi Licence
        </div>
        <div style="padding:10px 0;">
            <p style="font-size:1.1rem;">
                Your subscription expires on <strong>{expiry_display}</strong>.
                {'' if is_expired else f'<span style="color:var(--text-secondary);">Please renew your subscription before it expires.</span>'}
            </p>
            <a href="#" onclick="showInvoiceModal()" class="btn" style="margin-top:10px;">
                <i class="fas fa-file-invoice"></i> View Invoice & Payment Details
            </a>
        </div>
    </div>
    
    <!-- Payment History Table -->
    <div class="card">
        <div class="card-header">
            <i class="fas fa-history"></i> Payment History
            <div style="display:flex; gap:10px;">
                <input type="text" placeholder="Search..." style="width:200px; padding:6px 12px; border-radius:6px; border:1px solid var(--border); background:var(--bg); color:var(--text);">
                <button class="btn btn-small">Search</button>
            </div>
        </div>
        <div class="table-responsive" style="overflow-x:auto;">
            <table>
                <thead>
                    <tr>
                        <th>Amount</th>
                        <th>Payment Date</th>
                        <th>Status</th>
                        <th>Invoice</th>
                    </tr>
                </thead>
                <tbody>
                    {payment_rows}
                </tbody>
            </table>
        </div>
    </div>
    
    {invoice_modal}
    
    <script>
        function showInvoiceModal() {{
            document.getElementById('invoiceModal').style.display = 'block';
            document.body.style.overflow = 'hidden';
        }}
        
        function closeInvoiceModal() {{
            document.getElementById('invoiceModal').style.display = 'none';
            document.body.style.overflow = 'auto';
        }}
        
        window.onclick = function(event) {{
            var modal = document.getElementById('invoiceModal');
            if (event.target == modal) {{
                closeInvoiceModal();
            }}
        }}
    </script>
    '''
    
    return render_page("Billing & Subscription", content, get_pending_count(pid), pid, admin=True)
# ============================================================
# SUPER ADMIN
# ============================================================
SUPER_ADMIN_PASSWORD = 'rockabytech2025'

@app.route('/admin', methods=['GET','POST'])
def super_admin_login():
    if request.method == 'POST':
        if request.form.get('password') == SUPER_ADMIN_PASSWORD:
            session['super_admin'] = True
            db = get_db()
            db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1, 'super_admin_login', 'Super admin logged in')")
            db.commit()
            return redirect('/admin/dashboard')
        return render_page("Super Admin Login",'<div class="card"><div class="alert alert-error">Invalid password.</div><p><a href="/admin">Try again</a></p></div>',0,admin=False)
    return render_page("Super Admin Login",'<div class="card"><div class="card-header">🔐 RockabyTech Super Admin</div><form method="POST"><label>Password</label><input type="password" name="password" required><button type="submit" class="btn" style="margin-top:20px;width:100%;">Login</button></form></div>',0,admin=False)

@app.route('/admin/dashboard')
def super_admin_dashboard():
    if not session.get('super_admin'):
        return redirect('/admin')
    
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
        fee = int(total * 0.05)
        monthly_fee = int(this_month * 0.05)
        voucher_count = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=?", (p['id'],)).fetchone()['c']
        sub_status = "Active" if p['is_active'] else "Suspended"
        expiry = p['subscription_expiry'] if p['subscription_expiry'] else '-'
        expired = False
        if p['subscription_expiry'] and date.fromisoformat(p['subscription_expiry']) < date.today():
            sub_status = "Expired"
            expired = True
        row_class = 'style="background:rgba(255,212,59,0.1);"' if expired else ''
        
        rows += f'''
        <tr {row_class}>
            <td>{p['id']}</td>
            <td><strong>{p['business_name']}</strong></td>
            <td>{p['contact']}</td>
            <td><span class="badge" style="background:{'#51cf66' if sub_status=='Active' else '#ff6b6b' if sub_status=='Suspended' else '#ffd43b'};color:#000;padding:4px 10px;border-radius:12px;">{sub_status}</span></td>
            <td>UGX {total or 0:,}</td>
            <td>UGX {fee:,}</td>
            <td>UGX {monthly_fee:,}</td>
            <td>{voucher_count}</td>
            <td>{expiry}</td>
            <td style="overflow:visible; position:relative;">
                <div class="dropdown" style="position:relative;display:inline-block;z-index:9999;">
                    <button class="btn btn-small">&#8942;</button>
                    <div class="dropdown-content" style="display:none;position:absolute;right:0;top:100%;background:var(--card-bg);backdrop-filter:blur(20px);border-radius:8px;box-shadow:0 8px 25px rgba(0,0,0,0.2);z-index:999999;overflow:visible;padding:5px 0;min-width:200px;white-space:nowrap;">
                        <a href="/admin/impersonate/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-user-secret"></i> Impersonate</a>
                        <a href="/admin/extend/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-calendar-plus"></i> Extend</a>
                        <a href="/admin/edit-provider/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-edit"></i> Edit</a>
                        <a href="/admin/invoice/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-file-invoice"></i> Send Invoice</a>
                        <a href="/admin/message/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-envelope"></i> Message</a>
                        <a href="/admin/toggle-provider/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;"><i class="fas fa-power-off"></i> {('Suspend' if p['is_active'] else 'Activate')}</a>
                        <a href="/admin/delete-provider/{p['id']}" style="display:block;padding:10px 20px;color:var(--text);text-decoration:none;" onclick="return confirm('Delete permanently?')"><i class="fas fa-trash"></i> Delete</a>
                    </div>
                </div>
            </td>
        </tr>
        '''
    
    audit = db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 20").fetchall()
    audit_rows = ''.join(f'<tr><td>{a["created_at"][:16]}</td><td>{a["action"]}</td><td>{a["details"]}</td></tr>' for a in audit) or '<tr><td colspan="3">No activity yet.</td></tr>'
    
    content = f'''
    <div class="stat-grid">
        <div class="stat-card"><h3>{total_providers}</h3><small>Total Providers</small></div>
        <div class="stat-card"><h3>{active_providers}</h3><small>Active</small></div>
        <div class="stat-card"><h3>UGX {total_revenue or 0:,}</h3><small>Total Revenue</small></div>
        <div class="stat-card"><h3>UGX {platform_fee:,}</h3><small>Your 5% Fee</small></div>
        <div class="stat-card"><h3>{total_users}</h3><small>End Users</small></div>
        <div class="stat-card"><h3>{pending_approvals}</h3><small>Pending</small></div>
    </div>
    <div class="card">
        <div class="card-header">Today: UGX {today_revenue or 0:,} revenue | UGX {int(today_revenue * 0.05):,} your fee</div>
    </div>
    <div class="card">
        <div class="card-header">Provider Management <a href="/admin/add-provider" class="btn btn-success btn-small">+ Add Provider</a></div>
        <div class="table-responsive" style="overflow-x:auto; -webkit-overflow-scrolling:touch;">
            <table>
                <thead>
                    <tr>
                        <th>ID</th><th>Name</th><th>Contact</th><th>Status</th>
                        <th>Revenue</th><th>Total Fee</th><th>Fee/Mo</th>
                        <th>Vouchers</th><th>Expiry</th><th>Actions</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>
    <div class="card">
        <div class="card-header">🕒 Recent Activity</div>
        <div class="table-responsive" style="overflow-x:auto; -webkit-overflow-scrolling:touch;">
            <table>
                <thead><tr><th>Time</th><th>Action</th><th>Details</th></tr></thead>
                <tbody>{audit_rows}</tbody>
            </table>
        </div>
    </div>
    <p style="margin-top:20px;"><a href="/admin/logout" class="btn btn-outline">Logout</a></p>
    '''
    return render_page("Super Admin Dashboard", content, 0, admin=False)

# ===== SUPER ADMIN PROVIDER MANAGEMENT ROUTES =====

@app.route('/admin/add-provider', methods=['GET', 'POST'])
def add_provider():
    if not session.get('super_admin'):
        return redirect('/admin')
    
    if request.method == 'POST':
        db = get_db()
        hashed = generate_password_hash(request.form['password'])
        
        db.execute("""
            INSERT INTO providers (
                business_name, contact, password_hash, subscription_expiry, is_active,
                mtn_number, airtel_number, support_phone,
                percent_fee, monthly_fee_ugx
            ) VALUES (?,?,?,?,1,?,?,?,?,?)
        """, (
            request.form['business_name'],
            request.form['contact'],
            hashed,
            request.form['expiry'],
            request.form['mtn'],
            request.form['airtel'],
            request.form['support'],
            float(request.form['percent_fee']),
            int(request.form['monthly_fee'])
        ))
        
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        default_plans = [
            ('1 Hour', 60, 500),
            ('3 Hours', 180, 1000),
            ('24 Hours', 1440, 3000),
            ('Weekly', 10080, 10000),
            ('Monthly', 43200, 30000),
        ]
        for name, mins, price in default_plans:
            db.execute("""
                INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public, speed_down, speed_up)
                VALUES (?,?,?,?,1,'5M','2M')
            """, (new_id, name, mins, price))
        
        db.execute("""
            INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public, speed_down, speed_up)
            VALUES (?, 'Free Trial', 5, 0, 0, '1M', '512k')
        """, (new_id,))
        
        db.execute("INSERT INTO settings (provider_id, key, value) VALUES (?,'auto_approve','1')", (new_id,))
        db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'add_provider',?)",
                   (f"Added provider: {request.form['business_name']}",))
        db.commit()
        return redirect('/admin/dashboard')
    
    content = '''
    <div class="card">
        <div class="card-header">Add New Provider</div>
        <form method="POST">
            <label>Business Name*</label>
            <input type="text" name="business_name" required>
            <label>Contact Phone*</label>
            <input type="tel" name="contact" required>
            <label>Login Password*</label>
            <input type="password" name="password" required>
            <label>Subscription Expiry*</label>
            <input type="date" name="expiry" required>
            <label>MTN Number</label>
            <input type="text" name="mtn">
            <label>Airtel Number</label>
            <input type="text" name="airtel">
            <label>Support WhatsApp</label>
            <input type="text" name="support">
            <hr>
            <h4>Billing Settings</h4>
            <label>Platform Fee (%)</label>
            <input type="number" name="percent_fee" step="0.1" value="5" min="0" max="100">
            <small style="color:var(--text-secondary);">Percentage taken from each transaction (e.g., 5 for 5%)</small>
            <label>Monthly Maintenance Fee (UGX)</label>
            <input type="number" name="monthly_fee" value="20000" step="1000" min="0">
            <small style="color:var(--text-secondary);">Fixed monthly fee charged to the provider</small>
            <button type="submit" class="btn" style="margin-top:20px;">Create Provider</button>
        </form>
    </div>
    '''
    return render_page("Add Provider", content, 0, admin=False)

@app.route('/admin/edit-provider/<int:pid>', methods=['GET', 'POST'])
def edit_provider_admin(pid):
    if not session.get('super_admin'):
        return redirect('/admin')
    
    db = get_db()
    prov = db.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    if not prov:
        return redirect('/admin/dashboard')
    
    if request.method == 'POST':
        try:
            business_name = request.form.get('business_name', prov['business_name'])
            contact = request.form.get('contact', prov['contact'] or '')
            mtn = request.form.get('mtn', '')
            airtel = request.form.get('airtel', '')
            support = request.form.get('support', '')
            
            # percent_fee: allow 0, fallback only if empty string
            percent_fee_str = request.form.get('percent_fee', '').strip()
            if percent_fee_str == '':
                percent_fee = prov['percent_fee'] if prov['percent_fee'] is not None else 5.0
            else:
                percent_fee = float(percent_fee_str)
            
            # monthly_fee: allow 0, fallback only if empty string
            monthly_fee_str = request.form.get('monthly_fee', '').strip()
            if monthly_fee_str == '':
                monthly_fee = prov['monthly_fee_ugx'] if prov['monthly_fee_ugx'] is not None else 20000
            else:
                monthly_fee = int(monthly_fee_str)
            
            new_password = request.form.get('password', '')

            db.execute("""
                UPDATE providers SET
                    business_name=?, contact=?, mtn_number=?, airtel_number=?,
                    support_phone=?, percent_fee=?, monthly_fee_ugx=?
                WHERE id=?
            """, (business_name, contact, mtn, airtel, support, percent_fee, monthly_fee, pid))
            
            if new_password:
                db.execute("UPDATE providers SET password_hash=? WHERE id=?",
                           (generate_password_hash(new_password), pid))
            
            db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'edit_provider',?)",
                       (f"Edited provider: {business_name}",))
            db.commit()
            return redirect('/admin/dashboard')
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"Error updating provider: {str(e)}", 500
    
    # GET – display the form with current values
    percent_fee = prov['percent_fee'] if prov['percent_fee'] is not None else 5.0
    monthly_fee = prov['monthly_fee_ugx'] if prov['monthly_fee_ugx'] is not None else 20000
    
    content = f'''
    <div class="card">
        <div class="card-header">Edit Provider: {prov["business_name"]}</div>
        <form method="POST">
            <label>Business Name*</label>
            <input type="text" name="business_name" value="{prov["business_name"]}" required>
            <label>Contact Phone*</label>
            <input type="tel" name="contact" value="{prov["contact"] or ""}" required>
            <label>New Password (leave blank to keep current)</label>
            <input type="password" name="password">
            <label>MTN Number</label>
            <input type="text" name="mtn" value="{prov["mtn_number"] or ""}">
            <label>Airtel Number</label>
            <input type="text" name="airtel" value="{prov["airtel_number"] or ""}">
            <label>Support WhatsApp</label>
            <input type="text" name="support" value="{prov["support_phone"] or ""}">
            <hr>
            <h4>Billing Settings</h4>
            <label>Platform Fee (%)</label>
            <input type="number" name="percent_fee" step="0.1" value="{percent_fee}" min="0" max="100">
            <small style="color:var(--text-secondary);">Percentage taken from each transaction</small>
            <label>Monthly Maintenance Fee (UGX)</label>
            <input type="number" name="monthly_fee" value="{monthly_fee}" step="1000" min="0">
            <small style="color:var(--text-secondary);">Fixed monthly fee charged to the provider</small>
            <button type="submit" class="btn" style="margin-top:20px;">Save Changes</button>
        </form>
    </div>
    '''
    return render_page("Edit Provider", content, 0, admin=False)

@app.route('/admin/extend/<int:pid>', methods=['GET','POST'])
def extend_subscription(pid):
    if not session.get('super_admin'):
        return redirect('/admin')
    
    db = get_db()
    prov = db.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    if not prov:
        return redirect('/admin/dashboard')
    
    if request.method == 'POST':
        new_expiry = request.form['expiry']
        db.execute("UPDATE providers SET subscription_expiry=?, is_active=1 WHERE id=?", (new_expiry, pid))
        db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'extend_subscription',?)",
                   (f"Extended {prov['business_name']} to {new_expiry}",))
        db.commit()
        return redirect('/admin/dashboard')
    
    current = prov['subscription_expiry'] if prov['subscription_expiry'] else date.today().isoformat()
    content = f'''
    <div class="card">
        <div class="card-header">Extend Subscription for {prov["business_name"]}</div>
        <p>Current expiry: <strong>{current}</strong></p>
        <form method="POST">
            <label>New Expiry Date*</label>
            <input type="date" name="expiry" value="{current}" required>
            <div style="margin-top:10px;">
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry]').value='{(date.today()+timedelta(days=30)).isoformat()}'">+1 Month</button>
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry]').value='{(date.today()+timedelta(days=90)).isoformat()}'">+3 Months</button>
                <button type="button" class="btn btn-small" onclick="document.querySelector('[name=expiry]').value='{(date.today()+timedelta(days=365)).isoformat()}'">+1 Year</button>
            </div>
            <button type="submit" class="btn" style="margin-top:20px;">Save</button>
        </form>
    </div>
    '''
    return render_page("Extend Subscription", content, 0, admin=False)

@app.route('/admin/invoice/<int:pid>', methods=['GET','POST'])
def admin_invoice(pid):
    if not session.get('super_admin'):
        return redirect('/admin')
    
    db = get_db()
    prov = db.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    if not prov:
        return redirect('/admin/dashboard')
    
    if request.method == 'POST':
        inv_no = f"INV-{datetime.now().strftime('%Y%m')}-{random.randint(1000,9999)}"
        db.execute("""
            INSERT INTO invoices (provider_id, invoice_no, user_id, amount, status, due_date)
            VALUES (?,?,?,?,'pending',?)
        """, (pid, inv_no, request.form.get('user_id', 0), float(request.form['amount']), request.form['due_date']))
        db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'send_invoice',?)",
                   (f"Invoice {inv_no} for {prov['business_name']} - UGX {request.form['amount']}",))
        db.commit()
        return redirect('/admin/dashboard')
    
    content = f'''
    <div class="card">
        <div class="card-header">Send Invoice to {prov["business_name"]}</div>
        <form method="POST">
            <label>Amount (UGX)*</label>
            <input type="number" name="amount" step="0.01" required>
            <label>Due Date</label>
            <input type="date" name="due_date">
            <button type="submit" class="btn" style="margin-top:20px;">Send Invoice</button>
        </form>
    </div>
    '''
    return render_page("Send Invoice", content, 0, admin=False)

@app.route('/admin/message/<int:pid>', methods=['GET','POST'])
def admin_message(pid):
    if not session.get('super_admin'):
        return redirect('/admin')
    
    db = get_db()
    prov = db.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    if not prov:
        return redirect('/admin/dashboard')
    
    if request.method == 'POST':
        db.execute("INSERT INTO sms_log (provider_id, phone_number, message) VALUES (?,?,?)",
                   (pid, prov['contact'], request.form['message']))
        db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'send_message',?)",
                   (f"Message to {prov['business_name']}: {request.form['message'][:50]}",))
        db.commit()
        return redirect('/admin/dashboard')
    
    content = f'''
    <div class="card">
        <div class="card-header">Send Message to {prov["business_name"]} ({prov["contact"]})</div>
        <form method="POST">
            <label>Message*</label>
            <textarea name="message" rows="4" required></textarea>
            <button type="submit" class="btn" style="margin-top:20px;">Send</button>
        </form>
    </div>
    '''
    return render_page("Send Message", content, 0, admin=False)

@app.route('/admin/impersonate/<int:pid>')
def impersonate(pid):
    if not session.get('super_admin'):
        return redirect('/admin')
    
    db = get_db()
    prov = db.execute("SELECT * FROM providers WHERE id=?", (pid,)).fetchone()
    if prov:
        session['provider_id'] = prov['id']
        session['provider_name'] = prov['business_name']
        db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'impersonate',?)",
                   (f"Impersonated {prov['business_name']}",))
        db.commit()
        return redirect('/dashboard')
    return redirect('/admin/dashboard')

@app.route('/admin/toggle-provider/<int:pid>')
def toggle_provider(pid):
    if not session.get('super_admin'):
        return redirect('/admin')
    
    db = get_db()
    prov = db.execute("SELECT is_active, business_name FROM providers WHERE id=?", (pid,)).fetchone()
    if prov:
        new = 0 if prov['is_active'] else 1
        db.execute("UPDATE providers SET is_active=? WHERE id=?", (new, pid))
        db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'toggle_provider',?)",
                   (f"{'Activated' if new else 'Suspended'} {prov['business_name']}",))
        db.commit()
    return redirect('/admin/dashboard')

@app.route('/admin/delete-provider/<int:pid>')
def delete_provider(pid):
    if not session.get('super_admin'):
        return redirect('/admin')
    
    if pid == 1:
        return "Cannot delete the main admin provider.", 403
    
    db = get_db()
    prov = db.execute("SELECT business_name FROM providers WHERE id=?", (pid,)).fetchone()
    if prov:
        db.execute("DELETE FROM providers WHERE id=?", (pid,))
        db.execute("DELETE FROM plans WHERE provider_id=?", (pid,))
        db.execute("DELETE FROM vouchers WHERE provider_id=?", (pid,))
        db.execute("DELETE FROM voucher_requests WHERE provider_id=?", (pid,))
        db.execute("DELETE FROM subscribers WHERE provider_id=?", (pid,))
        db.execute("DELETE FROM settings WHERE provider_id=?", (pid,))
        db.execute("INSERT INTO audit_log (admin_id, action, details) VALUES (1,'delete_provider',?)",
                   (f"Deleted provider: {prov['business_name']}",))
        db.commit()
    return redirect('/admin/dashboard')

@app.route('/admin/logout')
def super_admin_logout():
    session.pop('super_admin', None)
    return redirect('/admin')

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
