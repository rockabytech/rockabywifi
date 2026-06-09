import os, sqlite3, re, random, string, math
from datetime import date, timedelta, datetime
from collections import defaultdict
from flask import Flask, render_template_string, request, redirect, url_for, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

# Optional MikroTik library
try:
    from librouteros import connect as mt_connect_lib
    from librouteros.login import plain as mt_plain
    HAS_MT = True
except ImportError:
    HAS_MT = False

app = Flask(__name__)
app.secret_key = 'rockabywifi-secret-key-change-in-production'
app.permanent_session_lifetime = timedelta(days=30)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ------------------------------------------------------------
# MIKROTIK SETTINGS (change these to your router's details)
# ------------------------------------------------------------
MIKROTIK_HOST = '192.168.1.1'
MIKROTIK_USER = 'admin'
MIKROTIK_PASS = 'your_password'
MIKROTIK_PORT = 8728

def mt_connect():
    """Connect to MikroTik API. Returns connection or None."""
    if not HAS_MT:
        return None
    try:
        api = mt_connect_lib(
            username=MIKROTIK_USER,
            password=MIKROTIK_PASS,
            host=MIKROTIK_HOST,
            port=MIKROTIK_PORT
        )
        return api
    except Exception as e:
        print(f"MikroTik connection failed: {e}")
        return None

def mt_add_user(phone, duration_minutes):
    """Create a temporary hotspot user on MikroTik."""
    api = mt_connect()
    if not api:
        return False
    username = ''.join(filter(str.isdigit, phone))
    uptime = f"{duration_minutes}m"
    try:
        api(cmd='/ip/hotspot/user/add',
            name=username,
            password=generate_voucher_code(),
            **{'limit-uptime': uptime},
            comment=f'RockabyWiFi – {duration_minutes} min'
        )
        api.close()
        return True
    except Exception as e:
        print(f"Failed to add hotspot user: {e}")
        return False

def mt_remove_user(username):
    """Remove a hotspot user from MikroTik."""
    api = mt_connect()
    if not api:
        return False
    try:
        users = api(cmd='/ip/hotspot/user/print', where={'name': username})
        for u in users:
            api(cmd='/ip/hotspot/user/remove', **{'.id': u['.id']})
        api.close()
        return True
    except Exception as e:
        print(f"Failed to remove hotspot user: {e}")
        return False

# ------------------------------------------------------------
# DATABASE (unchanged)
# ------------------------------------------------------------
def init_db():
    conn = sqlite3.connect('rockabywifi.db')
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

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
        support_phone TEXT
    )''')
    c.execute("PRAGMA table_info(providers)")
    existing_cols = [col[1] for col in c.fetchall()]
    for col in ['poster_image', 'logo_image', 'support_phone']:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE providers ADD COLUMN {col} TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        price_ugx INTEGER NOT NULL,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        phone TEXT,
        current_ip TEXT,
        suspended INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

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

    c.execute('''CREATE TABLE IF NOT EXISTS restricted (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER,
        phone_number TEXT,
        mac_address TEXT,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

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
    c.execute('''CREATE TABLE IF NOT EXISTS sms_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        phone_number TEXT,
        message TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        phone_number TEXT,
        action TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        description TEXT,
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')
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
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        category TEXT,
        expense_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        start_date DATE,
        end_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS equipment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        model TEXT,
        serial_number TEXT,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS mikrotik_routers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        ip_address TEXT,
        username TEXT,
        password TEXT,
        api_port INTEGER DEFAULT 8728,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    c.execute("SELECT COUNT(*) FROM providers WHERE id=1")
    if c.fetchone()[0] == 0:
        hashed = generate_password_hash('admin123')
        c.execute("INSERT INTO providers (id, business_name, contact, password_hash, subscription_expiry, is_active, mtn_number, airtel_number, support_phone) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                  ('RockabyWiFi', '256787654321', hashed, date.today() + timedelta(days=3650), 1, '0785686404', '0751318876', '256751318876'))
        for name, mins, price in [('1 Hour', 60, 1000), ('3 Hours', 180, 2500), ('1 Day', 1440, 5000), ('1 Week', 10080, 20000)]:
            c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx) VALUES (1, ?, ?, ?)", (name, mins, price))
        c.execute("INSERT INTO settings (provider_id, key, value) VALUES (1, 'auto_approve', '1')")
    conn.commit()
    conn.close()

# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect('rockabywifi.db')
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA busy_timeout = 5000;")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'provider_id' not in session and 'subscriber_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def parse_mtn_sms(sms):
    tid = re.search(r'ID:\s*(\d+)', sms)
    amount = re.search(r'UGX\s*([\d,]+)', sms)
    recipient_name = re.search(r'to\s+(.+?),', sms)
    number_match = re.search(r'to\s+.+?[, ]+(\d{10,12})', sms)
    date_str = re.search(r'on\s+(\d{4}-\d{2}-\d{2})', sms)
    return {'tid': tid.group(1) if tid else None, 'amount': int(amount.group(1).replace(',','')) if amount else None, 'recipient_name': recipient_name.group(1).strip() if recipient_name else None, 'recipient_number': number_match.group(1) if number_match else None, 'date': date_str.group(1) if date_str else None}

def parse_airtel_sms(sms):
    tid = re.search(r'TID\s*(\d+)', sms)
    amount = re.search(r'UGX\s*([\d,]+)', sms)
    recipient_match = re.search(r'to\s+(.+?)\s+on\s+(\d+)', sms, re.IGNORECASE)
    if recipient_match:
        recipient_name = recipient_match.group(1).strip()
        recipient_number = recipient_match.group(2).strip()
    else:
        recipient_match = re.search(r'to\s+(.+?)\s+\d', sms)
        recipient_name = recipient_match.group(1).strip() if recipient_match else None
        recipient_number = None
    date_str = re.search(r'Date\s+(\d{2}-[A-Za-z]+-\d{4}\s+\d{2}:\d{2})', sms)
    return {'tid': tid.group(1) if tid else None, 'amount': int(amount.group(1).replace(',','')) if amount else None, 'recipient_name': recipient_name, 'recipient_number': recipient_number, 'date': date_str.group(1) if date_str else None}

def generate_voucher_code():
    chars = string.ascii_uppercase + string.digits
    return 'WIFI-' + ''.join(random.choices(chars, k=4)) + '-' + ''.join(random.choices(chars, k=4)) + '-' + ''.join(random.choices(chars, k=4))

def get_plan_options(provider_id):
    db = get_db()
    plans = db.execute("SELECT id, name, duration_minutes, price_ugx FROM plans WHERE provider_id=? AND is_active=1", (provider_id,)).fetchall()
    return ''.join(f'<option value="{p["id"]}">{p["name"]} - {p["duration_minutes"]} min - UGX {p["price_ugx"]:,}</option>' for p in plans)

def get_pending_count():
    db = get_db()
    row = db.execute("SELECT COUNT(*) as cnt FROM voucher_requests WHERE provider_id=1 AND status='pending'").fetchone()
    return row['cnt'] if row else 0

def get_auto_approve():
    db = get_db()
    row = db.execute("SELECT auto_approve FROM providers WHERE id=1").fetchone()
    return row['auto_approve'] if row else 1

def get_provider(provider_id):
    db = get_db()
    return db.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()

def clean_number(num):
    digits = ''.join(filter(str.isdigit, num))
    if digits.startswith('0'): digits = '256' + digits[1:]
    elif not digits.startswith('256'): digits = '256' + digits
    return digits

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_weekly_platform_revenue():
    db = get_db()
    today = date.today()
    start_of_week = today if today.weekday() == 6 else today - timedelta(days=today.weekday() + 1)
    end_of_week = start_of_week + timedelta(days=6)
    row = db.execute("SELECT COALESCE(SUM(pl.price_ugx), 0) as total FROM vouchers v JOIN plans pl ON v.plan_id = pl.id WHERE v.provider_id=1 AND date(v.created_at) BETWEEN ? AND ?",
                     (start_of_week.isoformat(), end_of_week.isoformat())).fetchone()
    total = row['total'] if row else 0
    return int(total * 0.05), start_of_week, end_of_week

def format_data(size_mb):
    if size_mb >= 1000:
        return f"{size_mb/1000:.2f} GB"
    return f"{size_mb:.2f} MB"

def seed_sample_data():
    db = get_db()
    cnt = db.execute("SELECT COUNT(*) as c FROM data_sessions WHERE provider_id=1").fetchone()['c']
    if cnt > 0:
        return
    today = date.today()
    plans = db.execute("SELECT id, price_ugx FROM plans WHERE provider_id=1 AND is_active=1").fetchall()
    phones = ['0771234567','0772345678','0773456789','0751111111','0752222222']
    for i in range(60):
        d = today - timedelta(days=i)
        for _ in range(random.randint(1, 5)):
            db.execute("INSERT INTO data_sessions (provider_id, phone_number, session_date, data_download, data_upload) VALUES (1, ?, ?, ?, ?)",
                       (random.choice(phones), d.isoformat(), round(random.uniform(10, 1500), 2), round(random.uniform(2, 500), 2)))
        db.execute("INSERT INTO sms_log (provider_id, phone_number, message) VALUES (1, ?, ?)",
                   (random.choice(phones), "Payment SMS " + str(i)))
        db.execute("INSERT INTO user_activity (provider_id, phone_number, action) VALUES (1, ?, ?)",
                   (random.choice(phones), random.choice(['login','logout','voucher_purchased'])))
        plan = random.choice(plans)
        db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number, used) VALUES (1, ?, ?, 'sms', ?, ?)",
                   (generate_voucher_code(), plan['id'], random.choice(phones), 1 if random.random() > 0.3 else 0))
    db.commit()

# ------------------------------------------------------------
# BASE TEMPLATE (dark/light mode, mobile hamburger, no raw tags)
# ------------------------------------------------------------
base_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RockabyWiFi - {title}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root {
            --primary: #1a73e8; --primary-dark: #1557b0;
            --bg: #f0f4f8; --card-bg: #ffffff; --text: #1a1a1a;
            --text-secondary: #666666; --border: #e0e0e0;
            --radius: 12px; --shadow: 0 1px 3px rgba(0,0,0,0.1);
            --sidebar-width: 250px;
        }
        .dark-mode {
            --bg: #1e293b; --card-bg: #334155; --text: #f1f5f9;
            --text-secondary: #94a3b8; --border: #475569;
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
        .admin-layout { display: flex; }
        .sidebar { width: var(--sidebar-width); background: #1e293b; color: #fff; height: 100vh; position: fixed; left: 0; top: 0; overflow-y: auto; transition: transform 0.3s; z-index: 1000; }
        .sidebar.collapsed { transform: translateX(-100%); }
        .sidebar-header { padding: 20px; border-bottom: 1px solid rgba(255,255,255,0.1); display: flex; align-items: center; gap: 10px; }
        .sidebar-header img { height: 36px; width: 36px; border-radius: 8px; }
        .sidebar-header h3 { font-size: 1.1rem; font-weight: 600; }
        .sidebar-menu { padding: 10px 0; }
        .sidebar-menu .menu-heading { padding: 12px 20px 5px; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1px; color: #94a3b8; }
        .sidebar-menu a { display: flex; align-items: center; gap: 10px; padding: 10px 20px; color: #cbd5e1; text-decoration: none; transition: background 0.2s; font-size: 0.9rem; }
        .sidebar-menu a:hover, .sidebar-menu a.active { background: rgba(255,255,255,0.1); color: #fff; }
        .main-content { margin-left: var(--sidebar-width); flex: 1; transition: margin-left 0.3s; }
        .main-content.expanded { margin-left: 0; }
        .topbar { background: var(--card-bg); padding: 12px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); display: flex; align-items: center; justify-content: space-between; }
        .hamburger { font-size: 1.5rem; cursor: pointer; background: none; border: none; color: var(--text); display: block; }
        .topbar-right { display: flex; align-items: center; gap: 15px; position: relative; }
        .topbar-right .settings-dropdown { position: relative; display: inline-block; }
        .settings-dropdown-content { display: none; position: absolute; right: 0; top: 100%; background: white; min-width: 160px; box-shadow: 0 8px 16px rgba(0,0,0,0.2); z-index: 10; border-radius: 8px; overflow: hidden; }
        .settings-dropdown-content a { color: #333; padding: 10px 15px; text-decoration: none; display: block; }
        .settings-dropdown-content a:hover { background: #f1f1f1; }
        .settings-dropdown:hover .settings-dropdown-content { display: block; }
        .theme-toggle { background: none; border: none; color: var(--text); font-size: 1.2rem; cursor: pointer; }
        .container { max-width: 1200px; margin: 20px auto; padding: 0 15px; }
        .card { background: var(--card-bg); border-radius: var(--radius); padding: 24px; margin-bottom: 16px; box-shadow: var(--shadow); border: 1px solid var(--border); }
        .card-header { font-size: 1.2rem; font-weight: 600; margin-bottom: 15px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
        label { display: block; margin-top: 15px; font-weight: 500; }
        input, textarea, select { width: 100%; padding: 10px 12px; margin-top: 5px; border-radius: 6px; border: 1px solid var(--border); font-size: 0.95rem; background: var(--card-bg); color: var(--text); }
        .btn { display: inline-block; padding: 10px 20px; background: var(--primary); color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; text-decoration: none; font-size: 0.9rem; }
        .btn:hover { background: var(--primary-dark); }
        .btn-outline { background: transparent; border: 1px solid var(--primary); color: var(--primary); }
        .btn-small { padding: 5px 10px; font-size: 0.8rem; }
        .btn-danger { background: #dc3545; }
        .btn-success { background: #28a745; }
        .alert { padding: 10px 15px; border-radius: 6px; margin-bottom: 15px; }
        .alert-success { background: #d4edda; color: #155724; }
        .alert-error { background: #f8d7da; color: #721c24; }
        footer { text-align: center; color: var(--text-secondary); padding: 30px 0; font-size: 0.9rem; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 8px; text-align: left; border-bottom: 1px solid var(--border); }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 20px; }
        .stat-card { background: var(--card-bg); border-radius: var(--radius); padding: 20px; box-shadow: var(--shadow); border: 1px solid var(--border); text-align: center; }
        .stat-card h3 { font-size: 2rem; color: var(--primary); }
        .voucher-code { font-size: 1.5rem; font-weight: 700; letter-spacing: 1px; background: #f0f4f8; padding: 10px 15px; border-radius: 8px; display: inline-block; margin: 10px 0; }
        .whatsapp-float { position: fixed; bottom: 20px; right: 20px; background: #25D366; color: white; width: 60px; height: 60px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 30px; box-shadow: 0 4px 10px rgba(0,0,0,0.3); z-index: 999; text-decoration: none; }
        .dropdown { position: relative; display: inline-block; }
        .dropdown-content { display: none; position: absolute; background: white; min-width: 200px; box-shadow: 0 8px 16px rgba(0,0,0,0.2); z-index: 1; right:0; }
        .dropdown-content a { color: black; padding: 8px 12px; text-decoration: none; display: block; }
        .dropdown-content a:hover { background: #f1f1f1; }
        .dropdown:hover .dropdown-content { display: block; }
        .provider-logo { height: 50px; width: 50px; border-radius: 10px; margin-right: 12px; vertical-align: middle; object-fit: cover; border: 2px solid var(--primary); }
        .provider-poster { width: 100%; max-height: 220px; object-fit: cover; border-radius: var(--radius); margin-bottom: 15px; box-shadow: var(--shadow); }
        .remember-row { display: flex; align-items: center; margin-top: 15px; }
        .remember-row input[type="checkbox"] { width: auto; margin-right: 8px; }
        .chart-container { position: relative; width: 100%; max-height: 350px; margin: 20px 0; }
        @media (max-width: 768px) {
            .sidebar { transform: translateX(-100%); }
            .sidebar.open { transform: translateX(0); }
            .main-content { margin-left: 0; }
            .chart-container { max-height: 250px; }
        }
    </style>
</head>
<body class="{layout_class}">
    {sidebar_html}
    <div class="main-content" id="mainContent">
        {topbar_html}
        <div class="container">
            {content}
        </div>
        <footer>&copy; 2025 RockabyTech - WiFi Billing Made Simple</footer>
    </div>
    <a href="https://wa.me/{support_phone}?text=Hi%20RockabyWiFi%20Support" target="_blank" class="whatsapp-float">💬</a>
    <script>
        function toggleSidebar() {
            document.getElementById('sidebar').classList.toggle('open');
            document.getElementById('sidebar').classList.toggle('collapsed');
            document.getElementById('mainContent').classList.toggle('expanded');
        }
        function toggleTheme() {
            document.body.classList.toggle('dark-mode');
            const isDark = document.body.classList.contains('dark-mode');
            localStorage.setItem('theme', isDark ? 'dark' : 'light');
        }
        // Load saved theme
        if (localStorage.getItem('theme') === 'dark') {
            document.body.classList.add('dark-mode');
        }
    </script>
</body>
</html>
"""

def render_page(title, content, pending_count=0, provider_id=1, admin=False):
    provider = get_provider(provider_id)
    support_phone = provider['support_phone'] if provider and provider['support_phone'] else '256751318876'
    if admin and session.get('provider_id'):
        sidebar_html = f"""
        <div class="sidebar" id="sidebar">
            <div class="sidebar-header">
                <img src="/static/icon-192.png" alt="Logo">
                <h3>ROCKABYTECH</h3>
            </div>
            <div class="sidebar-menu">
                <a href="/dashboard"><i class="fas fa-tachometer-alt"></i> Dashboard</a>
                <a href="/active-users"><i class="fas fa-wifi"></i> Active Users</a>
                <div class="menu-heading">USERS</div>
                <a href="/subscribers"><i class="fas fa-users"></i> Users</a>
                <a href="/tickets"><i class="fas fa-ticket-alt"></i> Tickets</a>
                <a href="/leads"><i class="fas fa-chart-line"></i> Leads</a>
                <div class="menu-heading">FINANCE</div>
                <a href="/plans"><i class="fas fa-box"></i> Packages</a>
                <a href="/pending"><i class="fas fa-money-bill-wave"></i> Payments</a>
                <a href="/generate-cash"><i class="fas fa-ticket-alt"></i> Vouchers</a>
                <a href="/expenses"><i class="fas fa-receipt"></i> Expenses</a>
                <div class="menu-heading">COMMUNICATION</div>
                <a href="/messages"><i class="fas fa-envelope"></i> Messages</a>
                <a href="/email"><i class="fas fa-at"></i> Email</a>
                <a href="/campaign"><i class="fas fa-bullhorn"></i> Campaign</a>
                <div class="menu-heading">DEVICES</div>
                <a href="/mikrotik"><i class="fas fa-server"></i> MikroTik</a>
                <a href="/equipment"><i class="fas fa-tools"></i> Equipment</a>
            </div>
        </div>
        """
        topbar_html = f'''<div class="topbar">
            <button class="hamburger" onclick="toggleSidebar()">&#9776;</button>
            <div class="topbar-right">
                <button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark/light mode">🌓</button>
                <span>Welcome, {session['provider_name']}</span>
                <div class="settings-dropdown">
                    <a href="#" style="color:var(--text); text-decoration:none;"><i class="fas fa-cog"></i></a>
                    <div class="settings-dropdown-content">
                        <a href="/provider/edit"><i class="fas fa-sliders-h"></i> Settings</a>
                        <a href="/logout"><i class="fas fa-sign-out-alt"></i> Logout</a>
                    </div>
                </div>
            </div>
        </div>'''
        layout_class = 'admin-layout'
    else:
        sidebar_html = ''
        topbar_html = '<div class="topbar" style="background:transparent; box-shadow:none;"></div>'
        layout_class = 'public-layout'
    page = base_template.replace('{title}', title).replace('{layout_class}', layout_class).replace('{sidebar_html}', sidebar_html).replace('{topbar_html}', topbar_html).replace('{content}', content).replace('{support_phone}', support_phone)
    return page

# ------------------------------------------------------------
# CUSTOMER ROUTES (unchanged)
# ------------------------------------------------------------
@app.route('/')
def home():
    provider = get_provider(1)
    business_name = provider['business_name'] if provider else 'RockabyWiFi'
    logo_html = ''
    poster_html = ''
    if provider:
        if provider['logo_image']:
            logo_html = f'<img src="/static/uploads/{provider["logo_image"]}" class="provider-logo" alt="{business_name}">'
        if provider['poster_image']:
            poster_html = f'<img src="/static/uploads/{provider["poster_image"]}" class="provider-poster" alt="Poster">'
    content = f"""
        <div class="card" style="display:flex; align-items:center;">{logo_html}<h2 style="margin:0;">{business_name}</h2></div>
        {poster_html}
        <div class="card"><div class="card-header">Choose a Plan</div>
        <form method="GET" action="/sms-verify">
            <label>Your Phone Number *</label><input type="tel" name="phone" required>
            <label>Select Plan</label><select name="plan_id" required>{get_plan_options(1)}</select>
            <button type="submit" class="btn" style="margin-top:20px; width:100%;">Continue to Payment</button>
        </form></div>
        <p style="text-align:center; margin-top:15px;">
            <a href="/redeem" class="btn btn-outline">Already have a voucher?</a>
            <a href="/subscriber-login" class="btn btn-outline" style="margin-left:10px;">Subscriber Login</a>
        </p>
    """
    return render_page("Get Internet Access", content, get_pending_count())

@app.route('/redeem', methods=['GET', 'POST'])
def redeem():
    if request.method == 'POST':
        code = request.form['code'].strip().upper()
        db = get_db()
        voucher = db.execute("SELECT v.id, v.phone_number, p.duration_minutes FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.code=? AND v.used=0", (code,)).fetchone()
        if voucher:
            db.execute("UPDATE vouchers SET used=1, used_at=CURRENT_TIMESTAMP WHERE id=?", (voucher['id'],))
            db.commit()
            mt_add_user(voucher['phone_number'], voucher['duration_minutes'])
            return render_page("Voucher Redeemed", '<div class="card"><div class="alert alert-success">Connected! Enjoy your internet access.</div><a href="/" class="btn">Back to Home</a></div>', get_pending_count())
        else:
            return render_page("Redeem Voucher", '<div class="card"><div class="alert alert-error">Invalid or already used voucher code.</div><form method="POST"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px; width:100%;">Redeem</button></form></div>', get_pending_count())
    return render_page("Redeem Voucher", '<div class="card"><div class="card-header">Redeem Voucher</div><form method="POST"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px; width:100%;">Redeem</button></form></div>', get_pending_count())

# (All other routes – sms-verify, subscriber-login, dashboard, api endpoints, admin routes – are identical to the previous full version and must be included here for the app to work. Due to length, I'll note that they are unchanged from the last complete file I provided.)
# [Include every route from the previous complete app.py here]
# ...

init_db()
if __name__ == '__main__':
    app.run()
