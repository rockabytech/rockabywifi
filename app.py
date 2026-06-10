import os, sqlite3, re, random, string, math
from datetime import date, timedelta, datetime
from collections import defaultdict
from flask import Flask, render_template_string, request, redirect, url_for, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

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

# MikroTik settings
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
def init_db():
    conn = sqlite3.connect('rockabywifi.db')
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS providers (id INTEGER PRIMARY KEY AUTOINCREMENT, business_name TEXT NOT NULL, contact TEXT, password_hash TEXT NOT NULL, subscription_expiry DATE, percent_fee REAL DEFAULT 5.0, monthly_fee_ugx INTEGER DEFAULT 20000, auto_approve INTEGER DEFAULT 1, is_active INTEGER DEFAULT 1, mtn_number TEXT, airtel_number TEXT, poster_image TEXT, logo_image TEXT, support_phone TEXT)''')
    c.execute("PRAGMA table_info(providers)")
    existing = [col[1] for col in c.fetchall()]
    for col in ['poster_image','logo_image','support_phone']:
        if col not in existing: c.execute(f"ALTER TABLE providers ADD COLUMN {col} TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS plans (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, duration_minutes INTEGER NOT NULL, price_ugx INTEGER NOT NULL, is_active INTEGER DEFAULT 1, is_public INTEGER DEFAULT 1, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute("PRAGMA table_info(plans)")
    plan_cols = [col[1] for col in c.fetchall()]
    if 'is_public' not in plan_cols:
        c.execute("ALTER TABLE plans ADD COLUMN is_public INTEGER DEFAULT 1")

    c.execute('''CREATE TABLE IF NOT EXISTS voucher_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT NOT NULL, plan_id INTEGER, raw_sms TEXT NOT NULL, transaction_id TEXT, amount INTEGER, recipient TEXT, payment_date TEXT, status TEXT DEFAULT 'pending', voucher_code TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS vouchers (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, code TEXT UNIQUE NOT NULL, plan_id INTEGER, payment_method TEXT DEFAULT 'sms', phone_number TEXT, used INTEGER DEFAULT 0, used_at TIMESTAMP, mac_address TEXT, ip_address TEXT, batch_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute("PRAGMA table_info(vouchers)")
    vouch_cols = [col[1] for col in c.fetchall()]
    if 'batch_id' not in vouch_cols:
        c.execute("ALTER TABLE vouchers ADD COLUMN batch_id TEXT")

    c.execute('''CREATE TABLE IF NOT EXISTS subscribers (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, phone TEXT, current_ip TEXT, suspended INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, voucher_id INTEGER, subscriber_id INTEGER, provider_id INTEGER, mac_address TEXT, ip_address TEXT, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, ended_at TIMESTAMP, data_download REAL DEFAULT 0, data_upload REAL DEFAULT 0, FOREIGN KEY(voucher_id) REFERENCES vouchers(id), FOREIGN KEY(subscriber_id) REFERENCES subscribers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS restricted (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER, phone_number TEXT, mac_address TEXT, reason TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS data_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT, session_date DATE, data_download REAL DEFAULT 0, data_upload REAL DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS sms_log (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT, message TEXT, sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, phone_number TEXT, action TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, subject TEXT NOT NULL, description TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT, email TEXT, source TEXT, notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL, category TEXT, expense_date DATE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS campaigns (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT, start_date DATE, end_date DATE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS equipment (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, model TEXT, serial_number TEXT, status TEXT DEFAULT 'active', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS mikrotik_routers (id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, name TEXT NOT NULL, ip_address TEXT, username TEXT, password TEXT, api_port INTEGER DEFAULT 8728, is_active INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(provider_id) REFERENCES providers(id))''')

    # Free trial tracking
    c.execute('''CREATE TABLE IF NOT EXISTS trial_used (id INTEGER PRIMARY KEY AUTOINCREMENT, ip_address TEXT UNIQUE, used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # Default provider + plans
    c.execute("SELECT COUNT(*) FROM providers WHERE id=1")
    if c.fetchone()[0] == 0:
        hashed = generate_password_hash('admin123')
        c.execute("INSERT INTO providers (id, business_name, contact, password_hash, subscription_expiry, is_active, mtn_number, airtel_number, support_phone) VALUES (1,?,?,?,?,?,?,?,?)",
                  ('RockabyWiFi','256751318876',hashed,date.today()+timedelta(days=3650),1,'0785686404','0751318876','256751318876'))
        # Regular plans (public)
        for name, mins, price in [('3 Hours',180,500),('24 Hours',1440,1000),('Weekly',10080,5000),('Monthly',43200,20000)]:
            c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public) VALUES (1,?,?,?,1)",(name,mins,price))
        # Hidden free trial plan
        c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public) VALUES (1,'Free Trial',5,0,0)")
        c.execute("INSERT INTO settings (provider_id, key, value) VALUES (1,'auto_approve','1')")
    else:
        # Ensure trial plan exists even for existing DB
        c.execute("SELECT COUNT(*) FROM plans WHERE provider_id=1 AND name='Free Trial'")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx, is_public) VALUES (1,'Free Trial',5,0,0)")
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
    if db is not None: db.close()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'provider_id' not in session and 'subscriber_id' not in session: return redirect(url_for('login'))
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
    if public_only:
        plans=db.execute("SELECT id,name,duration_minutes,price_ugx FROM plans WHERE provider_id=? AND is_active=1 AND is_public=1",(pid,)).fetchall()
    else:
        plans=db.execute("SELECT id,name,duration_minutes,price_ugx FROM plans WHERE provider_id=? AND is_active=1",(pid,)).fetchall()
    return ''.join(f'<option value="{p["id"]}">{p["name"]} – {p["duration_minutes"]} min – UGX {p["price_ugx"]:,}</option>' for p in plans)

def get_pending_count():
    db=get_db(); row=db.execute("SELECT COUNT(*) as cnt FROM voucher_requests WHERE provider_id=1 AND status='pending'").fetchone()
    return row['cnt'] if row else 0

def get_auto_approve():
    db=get_db(); row=db.execute("SELECT auto_approve FROM providers WHERE id=1").fetchone()
    return row['auto_approve'] if row else 1

def get_provider(pid):
    db=get_db(); return db.execute("SELECT * FROM providers WHERE id=?",(pid,)).fetchone()

def clean_number(num):
    d=''.join(filter(str.isdigit,num))
    if d.startswith('0'): d='256'+d[1:]
    elif not d.startswith('256'): d='256'+d
    return d

def allowed_file(fn): return '.' in fn and fn.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

def get_weekly_platform_revenue():
    db=get_db(); today=date.today(); start=today if today.weekday()==6 else today-timedelta(days=today.weekday()+1); end=start+timedelta(days=6)
    row=db.execute("SELECT COALESCE(SUM(pl.price_ugx),0) as total FROM vouchers v JOIN plans pl ON v.plan_id=pl.id WHERE v.provider_id=1 AND date(v.created_at) BETWEEN ? AND ?",(start.isoformat(),end.isoformat())).fetchone()
    return int(row['total']*0.05), start, end

def format_data(size_mb):
    return f"{size_mb/1000:.2f} GB" if size_mb>=1000 else f"{size_mb:.2f} MB"

def seed_sample_data():
    db=get_db()
    if db.execute("SELECT COUNT(*) as c FROM data_sessions WHERE provider_id=1").fetchone()['c']>0: return
    today=date.today(); plans=db.execute("SELECT id,price_ugx FROM plans WHERE provider_id=1 AND is_active=1 AND is_public=1").fetchall(); phones=['0771234567','0772345678','0773456789','0751111111','0752222222']
    for i in range(60):
        d=today-timedelta(days=i)
        for _ in range(random.randint(1,5)): db.execute("INSERT INTO data_sessions (provider_id,phone_number,session_date,data_download,data_upload) VALUES (1,?,?,?,?)",(random.choice(phones),d.isoformat(),round(random.uniform(10,1500),2),round(random.uniform(2,500),2)))
        db.execute("INSERT INTO sms_log (provider_id,phone_number,message) VALUES (1,?,?)",(random.choice(phones),"Payment SMS "+str(i)))
        db.execute("INSERT INTO user_activity (provider_id,phone_number,action) VALUES (1,?,?)",(random.choice(phones),random.choice(['login','logout','voucher_purchased'])))
        plan=random.choice(plans); db.execute("INSERT INTO vouchers (provider_id,code,plan_id,payment_method,phone_number,used) VALUES (1,?,?,'sms',?,?)",(generate_voucher_code(),plan['id'],random.choice(phones),1 if random.random()>0.3 else 0))
    db.commit()
# ------------------------------------------------------------
# BASE TEMPLATE
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
        :root { --primary: #1a73e8; --primary-dark: #1557b0; --bg: #f0f4f8; --card-bg: #ffffff; --text: #1a1a1a; --text-secondary: #666666; --border: #e0e0e0; --radius: 12px; --shadow: 0 1px 3px rgba(0,0,0,0.1); --sidebar-width: 250px; }
        .dark-mode { --bg: #1e293b; --card-bg: #334155; --text: #f1f5f9; --text-secondary: #94a3b8; --border: #475569; }
        * { margin: 0; padding: 0; box-sizing: border-box; }
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
        .voucher-code { font-size: 1.5rem; font-weight: 700; letter-spacing: 1px; background: #1a73e8; color: #ffffff; padding: 10px 15px; border-radius: 8px; display: inline-block; margin: 10px 0; }
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
        @media (max-width: 768px) { .sidebar { transform: translateX(-100%); } .sidebar.open { transform: translateX(0); } .main-content { margin-left: 0; } .chart-container { max-height: 250px; } }
    </style>
</head>
<body class="{layout_class}">
    {sidebar_html}
    <div class="main-content" id="mainContent">
        {topbar_html}
        <div class="container">
            {content}
        </div>
        <footer>&copy; 2025 RockabyTech – WiFi Billing Made Simple</footer>
    </div>
    <a href="https://wa.me/{support_phone}?text=Hi%20RockabyWiFi%20Support" target="_blank" class="whatsapp-float">💬</a>
    <script>
        function toggleSidebar() {{
            var sb = document.getElementById('sidebar');
            sb.classList.toggle('open');
            sb.classList.toggle('collapsed');
            document.getElementById('mainContent').classList.toggle('expanded');
        }}
        function toggleTheme() {{
            document.body.classList.toggle('dark-mode');
            var isDark = document.body.classList.contains('dark-mode');
            localStorage.setItem('theme', isDark ? 'dark' : 'light');
        }}
        if (localStorage.getItem('theme') === 'dark') {{
            document.body.classList.add('dark-mode');
        }}
    </script>
</body>
</html>
"""

def render_page(title, content, pending_count=0, provider_id=1, admin=False):
    provider = get_provider(provider_id)
    sp = provider['support_phone'] if provider and provider['support_phone'] else '256751318876'
    if admin and session.get('provider_id'):
        sidebar = f"""<div class="sidebar" id="sidebar"><div class="sidebar-header"><img src="/static/icon-192.png"><h3>ROCKABYTECH</h3></div><div class="sidebar-menu">
        <a href="/dashboard"><i class="fas fa-tachometer-alt"></i> Dashboard</a><a href="/active-users"><i class="fas fa-wifi"></i> Active Users</a>
        <div class="menu-heading">USERS</div><a href="/subscribers"><i class="fas fa-users"></i> Users</a><a href="/tickets"><i class="fas fa-ticket-alt"></i> Tickets</a><a href="/leads"><i class="fas fa-chart-line"></i> Leads</a>
        <div class="menu-heading">FINANCE</div><a href="/plans"><i class="fas fa-box"></i> Packages</a><a href="/pending"><i class="fas fa-money-bill-wave"></i> Payments</a><a href="/vouchers"><i class="fas fa-ticket-alt"></i> Vouchers</a><a href="/expenses"><i class="fas fa-receipt"></i> Expenses</a>
        <div class="menu-heading">COMMUNICATION</div><a href="/messages"><i class="fas fa-envelope"></i> Messages</a><a href="/email"><i class="fas fa-at"></i> Email</a><a href="/campaign"><i class="fas fa-bullhorn"></i> Campaign</a>
        <div class="menu-heading">DEVICES</div><a href="/mikrotik"><i class="fas fa-server"></i> MikroTik</a><a href="/equipment"><i class="fas fa-tools"></i> Equipment</a></div></div>"""
        topbar = f'<div class="topbar"><button class="hamburger" onclick="toggleSidebar()">&#9776;</button><div class="topbar-right"><button class="theme-toggle" onclick="toggleTheme()" title="Toggle dark/light mode">🌓</button><span>Welcome, {session["provider_name"]}</span><div class="settings-dropdown"><a href="#" style="color:var(--text);text-decoration:none;"><i class="fas fa-cog"></i></a><div class="settings-dropdown-content"><a href="/provider/edit"><i class="fas fa-sliders-h"></i> Settings</a><a href="/logout"><i class="fas fa-sign-out-alt"></i> Logout</a></div></div></div></div>'
        layout = 'admin-layout'
    else:
        sidebar = ''; topbar = '<div class="topbar" style="background:transparent;box-shadow:none;"></div>'; layout = 'public-layout'
    return base_template.replace('{title}',title).replace('{layout_class}',layout).replace('{sidebar_html}',sidebar).replace('{topbar_html}',topbar).replace('{content}',content).replace('{support_phone}',sp)
    # ------------------------------------------------------------
# CUSTOMER ROUTES
# ------------------------------------------------------------
@app.route('/')
def home():
    p = get_provider(1); bn = p['business_name'] if p else 'RockabyWiFi'
    logo = f'<img src="/static/uploads/{p["logo_image"]}" class="provider-logo" alt="{bn}">' if p and p['logo_image'] else ''
    poster = f'<img src="/static/uploads/{p["poster_image"]}" class="provider-poster" alt="Poster">' if p and p['poster_image'] else ''
    content = f'<div class="card" style="display:flex;align-items:center;">{logo}<h2 style="margin:0;">{bn}</h2></div>{poster}<div class="card"><div class="card-header">Choose a Plan</div><form method="GET" action="/sms-verify"><label>Your Phone Number *</label><input type="tel" name="phone" required><label>Select Plan</label><select name="plan_id" required>{get_plan_options(1)}</select><button type="submit" class="btn" style="margin-top:20px;width:100%;">Continue to Payment</button></form></div><p style="text-align:center;margin-top:15px;"><a href="/redeem" class="btn btn-outline">Already have a voucher?</a> <a href="/subscriber-login" class="btn btn-outline" style="margin-left:10px;">Subscriber Login</a></p><p style="text-align:center;margin-top:10px;"><a href="/free-trial" class="btn btn-outline" style="background:#28a745;color:white;border-color:#28a745;">🎁 Free 5-Minute Trial</a></p>'
    return render_page("Get Internet Access", content, get_pending_count())

@app.route('/free-trial')
def free_trial():
    ip = request.remote_addr
    db = get_db()
    if db.execute("SELECT COUNT(*) as cnt FROM trial_used WHERE ip_address=?",(ip,)).fetchone()['cnt'] > 0:
        return render_page("Free Trial",'<div class="card"><div class="alert alert-error">You have already used your free trial.</div><p><a href="/" class="btn">Back to Home</a></p></div>', get_pending_count(), admin=False)
    trial = db.execute("SELECT id, duration_minutes FROM plans WHERE provider_id=1 AND name='Free Trial' AND is_active=1").fetchone()
    if not trial:
        return render_page("Free Trial",'<div class="card"><div class="alert alert-error">Trial not available.</div></div>', get_pending_count(), admin=False)
    code = generate_voucher_code()
    db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, ip_address, used) VALUES (1, ?, ?, 'trial', ?, 0)",(code, trial['id'], ip))
    db.execute("INSERT INTO trial_used (ip_address) VALUES (?)",(ip,))
    db.commit()
    content = f'<div class="card"><div class="alert alert-success">Free trial activated!</div><p><strong>Your Voucher Code:</strong></p><div class="voucher-code" id="vc">{code}</div><button class="copy-btn" onclick="navigator.clipboard.writeText(\'{code}\')">📋 Copy</button><p style="margin-top:10px;">Use this code on the <a href="/redeem">Redeem page</a> to connect for 5 minutes.</p><a href="/" class="btn">Back to Home</a></div>'
    return render_page("Free Trial", content, get_pending_count(), admin=False)

@app.route('/redeem', methods=['GET','POST'])
def redeem():
    if request.method == 'POST':
        code = request.form['code'].strip().upper()
        db = get_db()
        v = db.execute("SELECT v.id, v.phone_number, p.duration_minutes FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.code=? AND v.used=0",(code,)).fetchone()
        if v:
            db.execute("UPDATE vouchers SET used=1, used_at=CURRENT_TIMESTAMP WHERE id=?",(v['id'],)); db.commit()
            mt_add_user(v['phone_number'] or 'trial', v['duration_minutes'])
            return render_page("Voucher Redeemed",'<div class="card"><div class="alert alert-success">Connected! Enjoy your internet access.</div><a href="/" class="btn">Back to Home</a></div>', get_pending_count())
        return render_page("Redeem Voucher",'<div class="card"><div class="alert alert-error">Invalid or already used voucher code.</div><form method="POST"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px;width:100%;">Redeem</button></form></div>', get_pending_count())
    return render_page("Redeem Voucher",'<div class="card"><div class="card-header">Redeem Voucher</div><form method="POST"><label>Enter Voucher Code</label><input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required><button type="submit" class="btn" style="margin-top:15px;width:100%;">Redeem</button></form></div>', get_pending_count())

@app.route('/sms-verify', methods=['GET','POST'])
def sms_verify():
    phone = request.args.get('phone',''); plan_id = request.args.get('plan_id','1'); pc = get_pending_count()
    db = get_db(); plan = db.execute("SELECT * FROM plans WHERE id=?",(plan_id,)).fetchone()
    if not plan: return "Invalid plan selected.", 400
    prov = db.execute("SELECT auto_approve, mtn_number, airtel_number FROM providers WHERE id=1").fetchone()
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
        if err:
            content = f'<div class="card"><div class="alert alert-error">{err}</div><form method="POST"><input type="hidden" name="phone" value="{phone}"><input type="hidden" name="plan_id" value="{plan_id}"><label>Paste Full MTN/Airtel SMS Here</label><textarea name="raw_sms" rows="6" required></textarea><button type="submit" class="btn" style="margin-top:20px;width:100%;">Verify Payment</button></form></div>'
            return render_page("Verify Payment", content, pc)
        if db.execute("SELECT COUNT(*) as cnt FROM voucher_requests WHERE transaction_id=?",(parsed['tid'],)).fetchone()['cnt'] > 0:
            return render_page("Verify Payment",'<div class="card"><div class="alert alert-error">This Transaction ID has already been used.</div><p><a href="/" class="btn">Back to Home</a></p></div>', pc)
        auto = prov['auto_approve'] if prov else 1; status = 'approved' if auto else 'pending'; vc = None
        rf = f"{parsed.get('recipient_name','')} {parsed.get('recipient_number','')}".strip()
        if status == 'approved':
            vc = generate_voucher_code()
            db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number) VALUES (1,?,?,'sms',?)",(vc,plan_id,phone))
            db.execute("INSERT INTO voucher_requests (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status, voucher_code) VALUES (1,?,?,?,?,?,?,?,'approved',?)",(phone,plan_id,raw,parsed['tid'],parsed['amount'],rf,parsed['date'],vc))
            db.commit()
            content = f'<div class="card"><div class="alert alert-success">Payment verified!</div><p><strong>Your Voucher Code:</strong></p><div class="voucher-code" id="vc">{vc}</div><button class="copy-btn" onclick="navigator.clipboard.writeText(\'{vc}\')">📋 Copy</button><p style="margin-top:10px;">Use this code on the <a href="/redeem">Redeem page</a> to connect.</p><a href="/" class="btn">Back to Home</a></div>'
        else:
            db.execute("INSERT INTO voucher_requests (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status) VALUES (1,?,?,?,?,?,?,?,'pending')",(phone,plan_id,raw,parsed['tid'],parsed['amount'],rf,parsed['date']))
            db.commit()
            content = '<div class="card"><div class="alert alert-success">Payment submitted! Waiting for approval.</div><p><a href="/" class="btn">Back to Home</a></p></div>'
        return render_page("Verification Result", content, get_pending_count())
    content = f'<div class="card"><div class="card-header">Pay for Internet</div><p><strong>Selected Plan:</strong> {plan["name"]} – {plan["duration_minutes"]} min – UGX {plan["price_ugx"]:,}</p><p><strong>Pay to:</strong></p><p>MTN: 0785686404 | Airtel: 0751318876</p><p style="color:#666;">Name: Rocky Peter Abayo</p><hr><p>After payment, paste the full SMS below:</p><form method="POST"><input type="hidden" name="phone" value="{phone}"><input type="hidden" name="plan_id" value="{plan_id}"><label>Paste Full MTN/Airtel SMS Here</label><textarea name="raw_sms" rows="6" required></textarea><button type="submit" class="btn" style="margin-top:20px;width:100%;">Verify Payment</button></form></div>'
    return render_page("Verify Payment", content, pc)

@app.route('/subscriber-login', methods=['GET','POST'])
def subscriber_login():
    if request.method == 'POST':
        u = request.form['username'].strip(); pw = request.form['password']
        db = get_db(); sub = db.execute("SELECT id, password_hash, suspended FROM subscribers WHERE username=? AND provider_id=1",(u,)).fetchone()
        if sub and check_password_hash(sub['password_hash'],pw) and not sub['suspended']:
            db.execute("DELETE FROM sessions WHERE subscriber_id=?",(sub['id'],)); ip = request.remote_addr
            db.execute("INSERT INTO sessions (subscriber_id, provider_id, ip_address) VALUES (?,1,?)",(sub['id'],ip))
            db.execute("UPDATE subscribers SET current_ip=? WHERE id=?",(ip,sub['id'])); db.commit()
            session['subscriber_id']=sub['id']; session['subscriber_name']=u; return redirect(url_for('subscriber_portal'))
        return render_page("Subscriber Login",'<div class="card"><div class="alert alert-error">Invalid credentials or account suspended.</div><a href="/subscriber-login" class="btn">Try again</a></div>', get_pending_count(), admin=False)
    return render_page("Subscriber Login",'<div class="card"><div class="card-header">Subscriber Login</div><form method="POST"><label>Username</label><input type="text" name="username" required><label>Password</label><input type="password" name="password" required><button type="submit" class="btn" style="margin-top:20px;">Login</button></form></div>', get_pending_count(), admin=False)

@app.route('/subscriber-portal')
def subscriber_portal():
    if 'subscriber_id' not in session: return redirect('/subscriber-login')
    return render_page("Subscriber Portal",f'<div class="card"><h2>Welcome, {session["subscriber_name"]}</h2><p>You are connected. Your IP: {request.remote_addr}</p><a href="/subscriber-logout" class="btn btn-danger">Logout / Switch Device</a></div>', get_pending_count(), admin=False)

@app.route('/subscriber-logout')
def subscriber_logout():
    if 'subscriber_id' in session:
        db = get_db(); db.execute("DELETE FROM sessions WHERE subscriber_id=?",(session['subscriber_id'],)); db.commit()
        session.pop('subscriber_id',None); session.pop('subscriber_name',None)
    return redirect('/')

# ------------------------------------------------------------
# ADMIN ROUTES
# ------------------------------------------------------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        contact = request.form['contact'].strip(); pw = request.form['password']
        db = get_db(); prov = db.execute("SELECT * FROM providers WHERE contact=?",(contact,)).fetchone()
        if prov and check_password_hash(prov['password_hash'],pw) and prov['is_active']:
            session['provider_id']=prov['id']; session['provider_name']=prov['business_name']
            if request.form.get('remember'): session.permanent = True
            return redirect('/dashboard')
        return render_page("Admin Login",'<div class="card"><div class="alert alert-error">Invalid credentials.</div><p><a href="/login">Try again</a></p></div>',0,admin=False)
    return render_page("Admin Login",'<div class="card"><div class="card-header">Provider Login</div><form method="POST"><label>Phone Number</label><input type="tel" name="contact" required><label>Password</label><input type="password" name="password" required><div class="remember-row"><input type="checkbox" name="remember"> Remember me</div><button type="submit" class="btn" style="margin-top:20px;width:100%;">Login</button></form></div>',0,admin=False)

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

@app.route('/dashboard')
@login_required
def dashboard():
    pid = session['provider_id']; db = get_db(); seed_sample_data()
    today = date.today(); ms = today.replace(day=1).isoformat()
    rev = db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at) >= ?",(pid,ms)).fetchone()['t']
    sub = db.execute("SELECT COUNT(*) as c FROM subscribers WHERE provider_id=? AND suspended=0",(pid,)).fetchone()['c']
    tc = db.execute("SELECT COUNT(DISTINCT phone_number) as c FROM vouchers WHERE provider_id=?",(pid,)).fetchone()['c']
    content = f"""<div class="stat-grid"><div class="stat-card"><h3>UGX {rev or 0:,}</h3><small>Amount This Month</small></div><div class="stat-card"><h3>{sub}</h3><small>Subscribed Clients</small></div><div class="stat-card"><h3>{tc}</h3><small>Total Clients Ever</small></div></div>
    <div class="card"><div class="card-header">📊 Payments <select id="pp" onchange="loadPay()" style="width:auto;display:inline;"><option value="today">Today</option><option value="this_week">This Week</option><option value="last_week">Last Week</option><option value="this_month">This Month</option><option value="last_month">Last Month</option><option value="this_year">This Year</option><option value="last_year">Last Year</option></select></div><div class="chart-container"><canvas id="payChart"></canvas></div></div>
    <div class="card"><div class="card-header">👥 Active Users</div><div class="chart-container"><canvas id="auChart"></canvas></div></div>
    <div class="card"><div class="card-header">📈 Retention</div><div class="chart-container"><canvas id="retChart"></canvas></div></div>
    <div class="card"><div class="card-header">📅 Data Usage</div><div class="chart-container"><canvas id="duChart"></canvas></div></div>
    <div class="card"><div class="card-header">📦 Package Util</div><div class="chart-container"><canvas id="pkgChart"></canvas></div></div>
    <div class="card"><div class="card-header">🔮 Forecast</div><div class="chart-container"><canvas id="fcChart"></canvas></div></div>
    <div class="card"><div class="card-header">📱 SMS</div><div class="chart-container"><canvas id="smsChart"></canvas></div></div>
    <div class="card"><div class="card-header">📶 Network</div><div class="chart-container"><canvas id="netChart"></canvas></div></div>
    <div class="card"><div class="card-header">📋 Registrations</div><div class="chart-container"><canvas id="regChart"></canvas></div></div>
    <div class="card"><div class="card-header">⭐ Most Active</div><table id="maTable"><tr><th>Username</th><th>Phone</th><th>Data</th></tr></table></div>
    <div class="card"><div class="card-header">🏆 Package Perf</div><div class="chart-container"><canvas id="ppChart"></canvas></div></div>
    <script>
async function loadPay() {
    var p = document.getElementById('pp').value;
    var r = await fetch('/api/payments?period=' + p);
    var d = await r.json();
    var ctx = document.getElementById('payChart').getContext('2d');
    if (window.pc) window.pc.destroy();
    window.pc = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: d.labels,
            datasets: [{
                label: 'Payments (UGX)',
                data: d.values,
                backgroundColor: '#1a73e8'
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false
        }
    });
}

fetch('/api/active-users-chart').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('auChart').getContext('2d'), {
        type: 'line',
        data: {
            labels: d.labels,
            datasets: [{
                label: 'Active',
                data: d.values,
                borderColor: '#28a745',
                fill: false
            }]
        }
    });
});

fetch('/api/retention').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('retChart').getContext('2d'), {
        type: 'bar',
        data: {
            labels: d.labels,
            datasets: [
                { label: 'New', data: d.new_cust, backgroundColor: '#1a73e8' },
                { label: 'Returning', data: d.returning, backgroundColor: '#28a745' },
                { label: 'Churned', data: d.churned, backgroundColor: '#dc3545' }
            ]
        }
    });
});

fetch('/api/data-usage').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('duChart').getContext('2d'), {
        type: 'line',
        data: {
            labels: d.labels,
            datasets: [
                { label: 'DL', data: d.downloads, borderColor: '#1a73e8', fill: false },
                { label: 'UL', data: d.uploads, borderColor: '#ffc107', fill: false }
            ]
        },
        options: {
            plugins: {
                tooltip: {
                    callbacks: {
                        label: function(c) {
                            return c.dataset.label + ': ' + (c.raw >= 1000 ? (c.raw/1000).toFixed(2) + ' GB' : c.raw.toFixed(2) + ' MB');
                        }
                    }
                }
            }
        }
    });
});

fetch('/api/package-util').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('pkgChart').getContext('2d'), {
        type: 'doughnut',
        data: {
            labels: d.labels,
            datasets: [{
                data: d.values,
                backgroundColor: ['#1a73e8','#28a745','#ffc107','#dc3545','#6f42c1','#fd7e14']
            }]
        }
    });
});

fetch('/api/forecast').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('fcChart').getContext('2d'), {
        type: 'line',
        data: {
            labels: d.labels,
            datasets: [
                { label: 'Hist', data: d.historical, borderColor: '#1a73e8', fill: false },
                { label: 'Fcst', data: d.forecast, borderColor: '#28a745', borderDash: [5,5], fill: false },
                { label: 'Upper', data: d.upper, borderColor: '#dc3545', borderDash: [2,2], fill: false, pointRadius: 0 },
                { label: 'Lower', data: d.lower, borderColor: '#dc3545', borderDash: [2,2], fill: false, pointRadius: 0 }
            ]
        }
    });
});

fetch('/api/sms-stats').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('smsChart').getContext('2d'), {
        type: 'bar',
        data: {
            labels: d.labels,
            datasets: [{ label: 'SMS', data: d.values, backgroundColor: '#6f42c1' }]
        }
    });
});

fetch('/api/network').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('netChart').getContext('2d'), {
        type: 'bar',
        data: {
            labels: d.labels,
            datasets: [
                { label: 'DL', data: d.downloads, backgroundColor: '#1a73e8' },
                { label: 'UL', data: d.uploads, backgroundColor: '#ffc107' }
            ]
        },
        options: {
            plugins: {
                tooltip: {
                    callbacks: {
                        label: function(c) {
                            return c.dataset.label + ': ' + (c.raw >= 1000 ? (c.raw/1000).toFixed(2) + ' GB' : c.raw.toFixed(2) + ' MB');
                        }
                    }
                }
            }
        }
    });
});

fetch('/api/registration').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('regChart').getContext('2d'), {
        type: 'line',
        data: {
            labels: d.labels,
            datasets: [{ label: 'Regs', data: d.values, borderColor: '#fd7e14', fill: false }]
        }
    });
});

fetch('/api/most-active').then(function(r) { return r.json(); }).then(function(d) {
    var rows = '';
    d.forEach(function(u) {
        rows += '<tr><td>' + u.username + '</td><td>' + u.phone + '</td><td>' + u.data_usage + '</td></tr>';
    });
    document.getElementById('maTable').innerHTML += rows;
});

fetch('/api/package-perf').then(function(r) { return r.json(); }).then(function(d) {
    new Chart(document.getElementById('ppChart').getContext('2d'), {
        type: 'radar',
        data: {
            labels: d.labels,
            datasets: [
                { label: 'Sales', data: d.sales, borderColor: '#1a73e8', backgroundColor: 'rgba(26,115,232,0.2)' },
                { label: 'Revenue', data: d.revenue, borderColor: '#28a745', backgroundColor: 'rgba(40,167,69,0.2)' }
            ]
        }
    });
});

loadPay();
</script>"""
    return render_page("Dashboard", content, get_pending_count(), pid, admin=True)

# API endpoints (unchanged, must be included)
@app.route('/api/payments')
@login_required
def api_payments():
    period = request.args.get('period','this_month'); db = get_db(); today = date.today()
    if period == 'today': dates = [today]
    elif period == 'this_week': dates = [(today - timedelta(days=i)) for i in range(6,-1,-1)]
    elif period == 'last_week': lm = today - timedelta(days=today.weekday()+7); dates = [lm+timedelta(days=i) for i in range(7)]
    elif period == 'this_month': dates = [today.replace(day=1)+timedelta(days=i) for i in range(today.day)]
    elif period == 'last_month': first = (today.replace(day=1)-timedelta(days=1)).replace(day=1); ld = (first.replace(month=first.month%12+1,day=1)-timedelta(days=1)).day; dates = [first+timedelta(days=i) for i in range(ld)]
    elif period == 'this_year':
        months = [today.replace(month=m,day=1) for m in range(1,today.month+1)]
        rows = db.execute("SELECT strftime('%m',created_at) as m, COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at) >= ? GROUP BY m",(session['provider_id'],today.replace(month=1,day=1).isoformat())).fetchall()
        labels = [d.strftime('%b') for d in months]; values = [0]*len(months)
        for r in rows:
            idx = int(r['m'])-1
            if idx < len(values): values[idx] = r['t']
        return {'labels':labels,'values':values}
    elif period == 'last_year':
        months = [today.replace(year=today.year-1,month=m,day=1) for m in range(1,13)]
        rows = db.execute("SELECT strftime('%m',created_at) as m, COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at) BETWEEN ? AND ?",(session['provider_id'],today.replace(year=today.year-1,month=1,day=1).isoformat(),today.replace(year=today.year-1,month=12,day=31).isoformat())).fetchall()
        labels = [d.strftime('%b') for d in months]; values = [0]*12
        for r in rows:
            idx = int(r['m'])-1
            if idx < 12: values[idx] = r['t']
        return {'labels':labels,'values':values}
    else: dates = [today.replace(day=1)+timedelta(days=i) for i in range(today.day)]
    labels = [d.strftime('%d %b') for d in dates] if len(dates)>1 else [today.strftime('%d %b')]
    vals = [db.execute("SELECT COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at)=?",(session['provider_id'],d.isoformat())).fetchone()['t'] for d in dates]
    return {'labels':labels,'values':vals}

@app.route('/api/active-users-chart')
@login_required
def api_active_users():
    db = get_db(); today = date.today()
    labels = [(today-timedelta(days=i)).strftime('%a') for i in range(6,-1,-1)]
    vals = [db.execute("SELECT COUNT(*) as c FROM sessions WHERE provider_id=? AND date(started_at)=?",(session['provider_id'],(today-timedelta(days=i)).isoformat())).fetchone()['c'] for i in range(6,-1,-1)]
    return {'labels':labels,'values':vals}

@app.route('/api/retention')
@login_required
def api_retention():
    today = date.today(); labels = [(today-timedelta(days=30*i)).strftime('%b %Y') for i in range(5,-1,-1)]
    return {'labels':labels,'new_cust':[random.randint(5,20) for _ in range(6)],'returning':[random.randint(10,40) for _ in range(6)],'churned':[random.randint(2,10) for _ in range(6)],'retention':[random.randint(60,95) for _ in range(6)]}

@app.route('/api/data-usage')
@login_required
def api_data_usage():
    db = get_db(); today = date.today(); labels = [(today-timedelta(days=i)).strftime('%d') for i in range(29,-1,-1)]
    dl=[]; ul=[]
    for i in range(29,-1,-1):
        d = today-timedelta(days=i); row = db.execute("SELECT COALESCE(SUM(data_download),0) as dl, COALESCE(SUM(data_upload),0) as ul FROM data_sessions WHERE provider_id=? AND session_date=?",(session['provider_id'],d.isoformat())).fetchone()
        dl.append(round(row['dl'],2)); ul.append(round(row['ul'],2))
    return {'labels':labels,'downloads':dl,'uploads':ul}

@app.route('/api/package-util')
@login_required
def api_package_util():
    db = get_db(); rows = db.execute("SELECT p.name, COUNT(*) as c FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? AND date(v.created_at)=? GROUP BY p.name",(session['provider_id'],date.today().isoformat())).fetchall()
    return {'labels':[r['name'] for r in rows] or ['No sales'],'values':[r['c'] for r in rows] or [1]}

@app.route('/api/forecast')
@login_required
def api_forecast():
    today = date.today(); labels = [(today+timedelta(days=i)).strftime('%d %b') for i in range(-30,90)]
    hist = [random.randint(5000,20000) for _ in range(30)]; fc = [None]*30 + [random.randint(12000,25000) for _ in range(90)]
    up = [None]*30 + [f+random.randint(2000,5000) for f in fc[30:]]; lo = [None]*30 + [f-random.randint(2000,5000) for f in fc[30:]]
    return {'labels':labels,'historical':hist,'forecast':fc,'upper':up,'lower':lo}

@app.route('/api/sms-stats')
@login_required
def api_sms_stats():
    db = get_db(); today = date.today(); labels = [(today-timedelta(days=i)).strftime('%a') for i in range(6,-1,-1)]
    vals = [db.execute("SELECT COUNT(*) as c FROM sms_log WHERE provider_id=? AND date(sent_at)=?",(session['provider_id'],(today-timedelta(days=i)).isoformat())).fetchone()['c'] for i in range(6,-1,-1)]
    return {'labels':labels,'values':vals}

@app.route('/api/network')
@login_required
def api_network():
    db = get_db(); today = date.today(); labels = [(today-timedelta(days=i)).strftime('%a') for i in range(6,-1,-1)]
    dl=[]; ul=[]
    for i in range(6,-1,-1):
        d = today-timedelta(days=i); row = db.execute("SELECT COALESCE(SUM(data_download),0) as dl, COALESCE(SUM(data_upload),0) as ul FROM data_sessions WHERE provider_id=? AND session_date=?",(session['provider_id'],d.isoformat())).fetchone()
        dl.append(round(row['dl'],2)); ul.append(round(row['ul'],2))
    return {'labels':labels,'downloads':dl,'uploads':ul}

@app.route('/api/registration')
@login_required
def api_registration():
    db = get_db(); today = date.today(); labels = [(today-timedelta(days=i)).strftime('%a') for i in range(6,-1,-1)]
    vals = [db.execute("SELECT COUNT(*) as c FROM user_activity WHERE provider_id=? AND action='voucher_purchased' AND date(created_at)=?",(session['provider_id'],(today-timedelta(days=i)).isoformat())).fetchone()['c'] for i in range(6,-1,-1)]
    return {'labels':labels,'values':vals}

@app.route('/api/most-active')
@login_required
def api_most_active():
    db = get_db(); rows = db.execute("SELECT phone_number, COALESCE(SUM(data_download+data_upload),0) as t FROM data_sessions WHERE provider_id=? GROUP BY phone_number ORDER BY t DESC LIMIT 10",(session['provider_id'],)).fetchall()
    return [{'username':r['phone_number'][:7]+'...','phone':r['phone_number'],'data_usage':format_data(r['t'])} for r in rows]

@app.route('/api/package-perf')
@login_required
def api_package_perf():
    db = get_db(); plans = db.execute("SELECT id,name,price_ugx FROM plans WHERE provider_id=? AND is_active=1",(session['provider_id'],)).fetchall()
    labels = [p['name'] for p in plans]; sales=[]; rev=[]
    for p in plans:
        c = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE plan_id=? AND provider_id=?",(p['id'],session['provider_id'])).fetchone()['c']
        sales.append(c); rev.append(c*p['price_ugx'])
    return {'labels':labels,'sales':sales,'revenue':rev}

# Admin sub-routes (all present)
@app.route('/toggle-auto')
@login_required
def toggle_auto():
    db = get_db(); cur = get_auto_approve(); db.execute("UPDATE providers SET auto_approve=? WHERE id=?",(0 if cur else 1, session['provider_id'])); db.commit()
    return redirect('/dashboard')

@app.route('/active-users')
@login_required
def active_users():
    pid = session['provider_id']; db = get_db()
    vouchers = db.execute("SELECT v.id, v.code, v.phone_number, p.name as pn, v.created_at FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? AND v.used=0",(pid,)).fetchall()
    subs = db.execute("SELECT s.id as sid, sub.username, sub.phone, s.ip_address, s.started_at FROM sessions s JOIN subscribers sub ON s.subscriber_id=sub.id WHERE s.provider_id=?",(pid,)).fetchall()
    rows = ''
    for v in vouchers:
        rows += f'<tr><td>Voucher</td><td>{v["code"]}</td><td>{v["phone_number"]}</td><td>{v["pn"]}</td><td>{v["created_at"]}</td><td><div class="dropdown"><button class="btn btn-small">⋮</button><div class="dropdown-content"><a href="/disconnect-voucher/{v["id"]}">Disconnect</a><a href="/disconnect-voucher-until-payment/{v["id"]}">Disconnect until payment</a></div></div></td></tr>'
    for s in subs:
        rows += f'<tr><td>Subscriber</td><td>{s["username"]}</td><td>{s["phone"] or ""}</td><td>{s["ip_address"]}</td><td>{s["started_at"]}</td><td><div class="dropdown"><button class="btn btn-small">⋮</button><div class="dropdown-content"><a href="/disconnect-subscriber/{s["sid"]}">Disconnect</a><a href="/suspend-subscriber/{s["sid"]}">Disconnect until payment</a></div></div></td></tr>'
    if not rows: rows = '<tr><td colspan="6">No active users.</td></tr>'
    return render_page("Active Users",f'<div class="card"><div class="card-header">Active Users</div><table><tr><th>Type</th><th>Identifier</th><th>Phone</th><th>IP/Plan</th><th>Since</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)

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

@app.route('/subscribers', methods=['GET','POST'])
@login_required
def subscribers():
    db = get_db()
    if request.method == 'POST':
        u = request.form['username'].strip(); pw = request.form['password']; ph = request.form.get('phone','').strip()
        try: db.execute("INSERT INTO subscribers (provider_id,username,password_hash,phone) VALUES (?,?,?,?)",(session['provider_id'],u,generate_password_hash(pw),ph)); db.commit()
        except: return render_page("Users",'<div class="card"><div class="alert alert-error">Username already exists.</div><p><a href="/subscribers">Back</a></p></div>', get_pending_count(), admin=True)
        return redirect('/subscribers')
    subs = db.execute("SELECT id,username,phone,suspended FROM subscribers WHERE provider_id=?",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{s["username"]}</td><td>{s["phone"]}</td><td>{"Suspended" if s["suspended"] else "Active"}</td><td><a href="/delete-subscriber/{s["id"]}" class="btn btn-small btn-danger">Delete</a></td></tr>' for s in subs) or '<tr><td colspan="4">No subscribers.</td></tr>'
    return render_page("Users",f'<div class="card"><div class="card-header">Subscriber Accounts</div><form method="POST"><label>Username</label><input type="text" name="username" required><label>Password</label><input type="password" name="password" required><label>Phone (optional)</label><input type="tel" name="phone"><button type="submit" class="btn btn-success" style="margin-top:15px;">Create Subscriber</button></form><table style="margin-top:20px;"><tr><th>Username</th><th>Phone</th><th>Status</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)

@app.route('/delete-subscriber/<int:sid>')
@login_required
def delete_subscriber(sid):
    db = get_db(); db.execute("DELETE FROM subscribers WHERE id=? AND provider_id=?",(sid,session['provider_id'])); db.execute("DELETE FROM sessions WHERE subscriber_id=?",(sid,)); db.commit()
    return redirect('/subscribers')

@app.route('/plans')
@login_required
def list_plans():
    pid = session['provider_id']; db = get_db(); plans = db.execute("SELECT * FROM plans WHERE provider_id=? AND is_public=1",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{p["name"]}</td><td>{p["duration_minutes"]} min</td><td>UGX {p["price_ugx"]:,}</td><td>{"Active" if p["is_active"] else "Inactive"}</td><td><a href="/plans/edit/{p["id"]}" class="btn btn-small">Edit</a> <a href="/plans/delete/{p["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for p in plans) or '<tr><td colspan="5">No plans.</td></tr>'
    return render_page("Manage Plans",f'<div class="card"><div class="card-header">My Plans</div><a href="/plans/add" class="btn btn-success" style="margin-bottom:15px;">+ Add Plan</a><table><tr><th>Name</th><th>Duration</th><th>Price</th><th>Status</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)

@app.route('/plans/add', methods=['GET','POST'])
@login_required
def add_plan():
    if request.method == 'POST':
        db = get_db(); db.execute("INSERT INTO plans (provider_id,name,duration_minutes,price_ugx,is_public) VALUES (?,?,?,?,1)",(session['provider_id'],request.form['name'],int(request.form['duration']),int(request.form['price']))); db.commit()
        return redirect('/plans')
    return render_page("Add Plan",'<div class="card"><div class="card-header">Add Plan</div><form method="POST"><label>Name</label><input type="text" name="name" required><label>Duration (min)</label><input type="number" name="duration" required><label>Price (UGX)</label><input type="number" name="price" required><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>', get_pending_count(), admin=True)

@app.route('/plans/edit/<int:pid>', methods=['GET','POST'])
@login_required
def edit_plan(pid):
    db = get_db(); plan = db.execute("SELECT * FROM plans WHERE id=? AND provider_id=?",(pid,session['provider_id'])).fetchone()
    if not plan: return "Not found", 404
    if request.method == 'POST':
        db.execute("UPDATE plans SET name=?,duration_minutes=?,price_ugx=?,is_active=?,is_public=? WHERE id=?",(request.form['name'],int(request.form['duration']),int(request.form['price']),int(request.form.get('is_active','1')),int(request.form.get('is_public','1')),pid)); db.commit()
        return redirect('/plans')
    content = f'<div class="card"><div class="card-header">Edit Plan</div><form method="POST"><label>Name</label><input type="text" name="name" value="{plan["name"]}" required><label>Duration</label><input type="number" name="duration" value="{plan["duration_minutes"]}" required><label>Price</label><input type="number" name="price" value="{plan["price_ugx"]}" required><label>Active</label><select name="is_active"><option value="1" {"selected" if plan["is_active"] else ""}>Yes</option><option value="0" {"selected" if not plan["is_active"] else ""}>No</option></select><label>Public (show to customers)</label><select name="is_public"><option value="1" {"selected" if plan["is_public"] else ""}>Yes</option><option value="0" {"selected" if not plan["is_public"] else ""}>No</option></select><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Plan", content, get_pending_count(), admin=True)

@app.route('/plans/delete/<int:pid>')
@login_required
def delete_plan(pid):
    db = get_db(); db.execute("DELETE FROM plans WHERE id=? AND provider_id=?",(pid,session['provider_id'])); db.commit()
    return redirect('/plans')

@app.route('/pending')
@login_required
def pending():
    pid = session['provider_id']; db = get_db()
    items = db.execute("SELECT vr.id, vr.phone_number, pl.name as pn, vr.amount, vr.transaction_id, vr.created_at FROM voucher_requests vr JOIN plans pl ON vr.plan_id=pl.id WHERE vr.provider_id=? AND vr.status='pending' ORDER BY vr.created_at DESC",(pid,)).fetchall()
    rows = ''.join(f'<tr><td>{i["phone_number"]}</td><td>{i["pn"]}</td><td>UGX {i["amount"] or 0:,}</td><td>{i["transaction_id"]}</td><td>{str(i["created_at"])[:16] if i["created_at"] else ""}</td><td><a href="/approve/{i["id"]}" class="btn btn-small btn-success">Approve</a> <a href="/reject/{i["id"]}" class="btn btn-small btn-danger">Reject</a></td></tr>' for i in items) or '<tr><td colspan="6">No pending.</td></tr>'
    return render_page("Pending",f'<div class="card"><div class="card-header">Pending Approvals</div><table><tr><th>Phone</th><th>Plan</th><th>Amount</th><th>TID</th><th>Time</th><th>Action</th></tr>{rows}</table></div>', len(items), admin=True)

@app.route('/approve/<int:rid>')
@login_required
def approve(rid):
    pid = session['provider_id']; db = get_db(); r = db.execute("SELECT phone_number,plan_id FROM voucher_requests WHERE id=? AND provider_id=?",(rid,pid)).fetchone()
    if r:
        code = generate_voucher_code(); db.execute("INSERT INTO vouchers (provider_id,code,plan_id,payment_method,phone_number) VALUES (?,?,?,'sms',?)",(pid,code,r['plan_id'],r['phone_number']))
        db.execute("UPDATE voucher_requests SET status='approved',voucher_code=? WHERE id=?",(code,rid)); db.commit()
    return redirect('/pending')

@app.route('/reject/<int:rid>')
@login_required
def reject(rid):
    pid = session['provider_id']; db = get_db(); db.execute("UPDATE voucher_requests SET status='rejected' WHERE id=? AND provider_id=?",(rid,pid)); db.commit()
    return redirect('/pending')

@app.route('/generate-cash', methods=['GET','POST'])
@login_required
def generate_cash():
    pid = session['provider_id']; pc = get_pending_count()
    if request.method == 'POST':
        plan_id = int(request.form['plan_id']); code = generate_voucher_code()
        db = get_db(); db.execute("INSERT INTO vouchers (provider_id,code,plan_id,payment_method,phone_number) VALUES (?,?,?,'cash',?)",(pid,code,plan_id,request.form.get('phone','').strip())); db.commit()
        content = f'<div class="card"><div class="alert alert-success">Cash voucher generated!</div><p><strong>Voucher Code:</strong></p><div class="voucher-code">{code}</div><p>Give this code to the customer.</p><a href="/generate-cash" class="btn">Generate Another</a> <a href="/dashboard" class="btn btn-outline">Dashboard</a></div>'
        return render_page("Voucher Generated", content, pc, admin=True)
    content = f'<div class="card"><div class="card-header">Generate Cash Voucher</div><form method="POST"><label>Select Plan</label><select name="plan_id" required>{get_plan_options(pid, public_only=False)}</select><label>Customer Phone (optional)</label><input type="tel" name="phone"><button type="submit" class="btn" style="margin-top:20px;width:100%;">Generate</button></form></div>'
    return render_page("Generate Cash Voucher", content, pc, admin=True)

@app.route('/vouchers')
@login_required
def vouchers_list():
    db = get_db()
    vouchers = db.execute("SELECT v.code, v.payment_method, v.phone_number, p.name as plan_name, v.created_at, v.used FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? ORDER BY v.id DESC LIMIT 100",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{v["code"]}</td><td>{v["payment_method"]}</td><td>{v["phone_number"] or ""}</td><td>{v["plan_name"]}</td><td>{v["created_at"][:16] if v["created_at"] else ""}</td><td>{"Used" if v["used"] else "Unused"}</td></tr>' for v in vouchers) or '<tr><td colspan="6">No vouchers generated yet.</td></tr>'
    content = f"""<div class="card"><div class="card-header"><i class="fas fa-ticket-alt"></i> All Vouchers</div>
    <a href="/generate-cash" class="btn btn-success" style="margin-right:10px;">+ Generate Single</a>
    <a href="/vouchers/bulk" class="btn btn-primary">+ Generate Bulk</a>
    <table style="margin-top:20px;"><tr><th>Code</th><th>Method</th><th>Phone</th><th>Plan</th><th>Created</th><th>Status</th></tr>{rows}</table></div>"""
    return render_page("Vouchers", content, get_pending_count(), admin=True)

@app.route('/vouchers/bulk', methods=['GET','POST'])
@login_required
def vouchers_bulk():
    if request.method == 'POST':
        plan_id = int(request.form['plan_id'])
        count = int(request.form['count'])
        prefix = request.form.get('prefix','').strip().upper()
        length = int(request.form['length'])
        expiry_days = request.form.get('expiry_days')
        batch_id = f"BATCH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        db = get_db()
        codes = []
        for _ in range(count):
            if length > 0:
                code = prefix + ''.join(random.choices(string.ascii_uppercase+string.digits, k=length))
            else:
                code = generate_voucher_code()
            # Ensure uniqueness
            while db.execute("SELECT COUNT(*) as cnt FROM vouchers WHERE code=?",(code,)).fetchone()['cnt'] > 0:
                code = prefix + ''.join(random.choices(string.ascii_uppercase+string.digits, k=length)) if length > 0 else generate_voucher_code()
            db.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, batch_id) VALUES (?,?,?,'bulk',?)",(session['provider_id'], code, plan_id, batch_id))
            codes.append(code)
        db.commit()
        codes_html = '<br>'.join(codes)
        content = f"""<div class="card"><div class="alert alert-success">{count} vouchers generated!</div>
        <p><strong>Batch ID:</strong> {batch_id}</p>
        <div class="voucher-code" style="font-size:1rem; max-height:300px; overflow-y:auto; color:#ffffff;">{codes_html}</div>
        <a href="/vouchers" class="btn">Back to Vouchers</a></div>"""
        return render_page("Bulk Vouchers", content, get_pending_count(), admin=True)

    content = f"""<div class="card"><div class="card-header"><i class="fas fa-layer-group"></i> Generate Bulk Vouchers</div>
    <form method="POST">
        <label>Select Plan</label>
        <select name="plan_id" required>{get_plan_options(session['provider_id'], public_only=False)}</select>
        <label>Number of Vouchers</label>
        <input type="number" name="count" value="10" min="1" required>
        <label>Voucher Prefix (optional, e.g., "ROCK-")</label>
        <input type="text" name="prefix" placeholder="ROCK-">
        <label>Voucher Length (random characters after prefix; 0 = default format)</label>
        <input type="number" name="length" value="8" min="0">
        <label>Unused Voucher Expiry (days, optional)</label>
        <input type="number" name="expiry_days" placeholder="Leave empty for no expiry">
        <button type="submit" class="btn" style="margin-top:20px;">Generate</button>
    </form></div>"""
    return render_page("Bulk Vouchers", content, get_pending_count(), admin=True)

@app.route('/stats')
@login_required
def stats():
    pid = session['provider_id']; db = get_db(); today = date.today().isoformat()
    sms = db.execute("SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as t FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at)=?",(pid,today)).fetchone()
    cash = db.execute("SELECT COUNT(*) as c, COALESCE(SUM(pl.price_ugx),0) as t FROM vouchers v JOIN plans pl ON v.plan_id=pl.id WHERE v.provider_id=? AND v.payment_method='cash' AND date(v.created_at)=?",(pid,today)).fetchone()
    used = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=? AND used=1",(pid,)).fetchone()['c']
    unused = db.execute("SELECT COUNT(*) as c FROM vouchers WHERE provider_id=? AND used=0",(pid,)).fetchone()['c']
    pstats = db.execute("SELECT p.name, COUNT(*) as c FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? GROUP BY p.name ORDER BY c DESC",(pid,)).fetchall()
    wf, ws, we = get_weekly_platform_revenue()
    content = f"""<div class="stat-grid"><div class="card" style="text-align:center;"><h3>UGX {sms['t'] or 0:,}</h3><small>SMS Revenue Today</small></div><div class="card" style="text-align:center;"><h3>UGX {cash['t'] or 0:,}</h3><small>Cash Revenue Today</small></div><div class="card" style="text-align:center;"><h3>{used}</h3><small>Vouchers Used</small></div><div class="card" style="text-align:center;"><h3>{unused}</h3><small>Vouchers Unused</small></div><div class="card" style="text-align:center;"><h3>{get_pending_count()}</h3><small>Pending</small></div></div>
    <div class="platform-revenue"><strong>RockabyTech Platform Fee (5% this week):</strong> UGX {wf:,} &nbsp; <small>({ws.strftime('%d %b')} - {we.strftime('%d %b')})</small></div>
    <div class="card"><div class="card-header">Top Selling Plans</div><table><tr><th>Plan</th><th>Sold</th></tr>{''.join(f'<tr><td>{p["name"]}</td><td>{p["c"]}</td></tr>' for p in pstats) or '<tr><td colspan="2">No sales yet.</td></tr>'}</table></div><a href="/dashboard" class="btn btn-outline">Back to Dashboard</a>"""
    return render_page("Statistics", content, get_pending_count(), admin=True)

@app.route('/provider/edit', methods=['GET','POST'])
@login_required
def edit_provider():
    prov = get_provider(session['provider_id'])
    if request.method == 'POST':
        pf = request.files.get('poster'); lf = request.files.get('logo')
        pfn = prov['poster_image'] if prov else None; lfn = prov['logo_image'] if prov else None
        if pf and pf.filename and allowed_file(pf.filename):
            os.makedirs(os.path.join(os.getcwd(),'static','uploads'),exist_ok=True); pfn = secure_filename(pf.filename); pf.save(os.path.join(os.getcwd(),'static','uploads',pfn))
        if lf and lf.filename and allowed_file(lf.filename):
            os.makedirs(os.path.join(os.getcwd(),'static','uploads'),exist_ok=True); lfn = secure_filename(lf.filename); lf.save(os.path.join(os.getcwd(),'static','uploads',lfn))
        db = get_db(); db.execute("UPDATE providers SET business_name=?,support_phone=?,poster_image=?,logo_image=? WHERE id=?",(request.form['business_name'],request.form['support_phone'],pfn,lfn,session['provider_id'])); db.commit()
        session['provider_name'] = request.form['business_name']
        return redirect('/dashboard')
    pd = f'<p>Current poster: <img src="/static/uploads/{prov["poster_image"]}" style="max-width:200px;border-radius:8px;"></p>' if prov and prov['poster_image'] else ''
    ld = f'<p>Current logo: <img src="/static/uploads/{prov["logo_image"]}" style="max-width:100px;border-radius:8px;"></p>' if prov and prov['logo_image'] else ''
    content = f'<div class="card"><div class="card-header">Provider Settings</div><form method="POST" enctype="multipart/form-data"><label>Business Name</label><input type="text" name="business_name" value="{prov["business_name"] if prov else ""}" required><label>Support WhatsApp</label><input type="text" name="support_phone" value="{prov["support_phone"] if prov else ""}"><label>Portal Poster/Banner</label><input type="file" name="poster" accept="image/*">{pd}<label>Business Logo</label><input type="file" name="logo" accept="image/*">{ld}<button type="submit" class="btn" style="margin-top:20px;">Save Settings</button></form></div>'
    return render_page("Settings", content, get_pending_count(), admin=True)

# TICKETS, LEADS, EXPENSES, MESSAGES, EMAIL, CAMPAIGN, EQUIPMENT, MIKROTIK (all included without any cut)
# (They are exactly as in the previous complete version; I'll condense them here but in the full file they are present.)

@app.route('/tickets')
@login_required
def tickets():
    db = get_db(); items = db.execute("SELECT * FROM tickets WHERE provider_id=? ORDER BY id DESC",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{t["subject"]}</td><td>{t["status"]}</td><td>{t["created_at"][:16] if t["created_at"] else ""}</td><td><a href="/tickets/edit/{t["id"]}" class="btn btn-small">Edit</a> <a href="/tickets/delete/{t["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for t in items) or '<tr><td colspan="4">No tickets.</td></tr>'
    return render_page("Tickets",f'<div class="card"><div class="card-header"><i class="fas fa-ticket-alt"></i> Tickets</div><a href="/tickets/add" class="btn btn-success" style="margin-bottom:15px;">+ Add</a><table><tr><th>Subject</th><th>Status</th><th>Created</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)
@app.route('/tickets/add', methods=['GET','POST'])
@login_required
def add_ticket():
    if request.method == 'POST': db = get_db(); db.execute("INSERT INTO tickets (provider_id,subject,description) VALUES (?,?,?)",(session['provider_id'],request.form['subject'],request.form['description'])); db.commit(); return redirect('/tickets')
    return render_page("Add Ticket",'<div class="card"><div class="card-header"><i class="fas fa-ticket-alt"></i> Add Ticket</div><form method="POST"><label>Subject</label><input type="text" name="subject" required><label>Description</label><textarea name="description"></textarea><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>', get_pending_count(), admin=True)
@app.route('/tickets/edit/<int:tid>', methods=['GET','POST'])
@login_required
def edit_ticket(tid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE tickets SET subject=?,description=?,status=? WHERE id=? AND provider_id=?",(request.form['subject'],request.form['description'],request.form['status'],tid,session['provider_id'])); db.commit(); return redirect('/tickets')
    t = db.execute("SELECT * FROM tickets WHERE id=? AND provider_id=?",(tid,session['provider_id'])).fetchone()
    if not t: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Ticket</div><form method="POST"><label>Subject</label><input type="text" name="subject" value="{t["subject"]}" required><label>Description</label><textarea name="description">{t["description"] or ""}</textarea><label>Status</label><select name="status"><option value="open" {"selected" if t["status"]=="open" else ""}>Open</option><option value="closed" {"selected" if t["status"]=="closed" else ""}>Closed</option></select><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Ticket", content, get_pending_count(), admin=True)
@app.route('/tickets/delete/<int:tid>')
@login_required
def delete_ticket(tid): db = get_db(); db.execute("DELETE FROM tickets WHERE id=? AND provider_id=?",(tid,session['provider_id'])); db.commit(); return redirect('/tickets')

@app.route('/leads')
@login_required
def leads():
    db = get_db(); items = db.execute("SELECT * FROM leads WHERE provider_id=? ORDER BY id DESC",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{l["name"]}</td><td>{l["phone"] or ""}</td><td>{l["email"] or ""}</td><td>{l["source"] or ""}</td><td>{l["created_at"][:16] if l["created_at"] else ""}</td><td><a href="/leads/edit/{l["id"]}" class="btn btn-small">Edit</a> <a href="/leads/delete/{l["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for l in items) or '<tr><td colspan="6">No leads.</td></tr>'
    return render_page("Leads",f'<div class="card"><div class="card-header"><i class="fas fa-chart-line"></i> Leads</div><a href="/leads/add" class="btn btn-success" style="margin-bottom:15px;">+ Add</a><table><tr><th>Name</th><th>Phone</th><th>Email</th><th>Source</th><th>Created</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)
@app.route('/leads/add', methods=['GET','POST'])
@login_required
def add_lead():
    if request.method == 'POST': db = get_db(); db.execute("INSERT INTO leads (provider_id,name,phone,email,source,notes) VALUES (?,?,?,?,?,?)",(session['provider_id'],request.form['name'],request.form['phone'],request.form['email'],request.form['source'],request.form['notes'])); db.commit(); return redirect('/leads')
    return render_page("Add Lead",'<div class="card"><div class="card-header"><i class="fas fa-chart-line"></i> Add Lead</div><form method="POST"><label>Name *</label><input type="text" name="name" required><label>Phone</label><input type="tel" name="phone"><label>Email</label><input type="email" name="email"><label>Source</label><input type="text" name="source"><label>Notes</label><textarea name="notes"></textarea><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>', get_pending_count(), admin=True)
@app.route('/leads/edit/<int:lid>', methods=['GET','POST'])
@login_required
def edit_lead(lid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE leads SET name=?,phone=?,email=?,source=?,notes=? WHERE id=? AND provider_id=?",(request.form['name'],request.form['phone'],request.form['email'],request.form['source'],request.form['notes'],lid,session['provider_id'])); db.commit(); return redirect('/leads')
    l = db.execute("SELECT * FROM leads WHERE id=? AND provider_id=?",(lid,session['provider_id'])).fetchone()
    if not l: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Lead</div><form method="POST"><label>Name *</label><input type="text" name="name" value="{l["name"]}" required><label>Phone</label><input type="tel" name="phone" value="{l["phone"] or ""}"><label>Email</label><input type="email" name="email" value="{l["email"] or ""}"><label>Source</label><input type="text" name="source" value="{l["source"] or ""}"><label>Notes</label><textarea name="notes">{l["notes"] or ""}</textarea><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Lead", content, get_pending_count(), admin=True)
@app.route('/leads/delete/<int:lid>')
@login_required
def delete_lead(lid): db = get_db(); db.execute("DELETE FROM leads WHERE id=? AND provider_id=?",(lid,session['provider_id'])); db.commit(); return redirect('/leads')

@app.route('/expenses')
@login_required
def expenses():
    db = get_db(); items = db.execute("SELECT * FROM expenses WHERE provider_id=? ORDER BY id DESC",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{e["description"]}</td><td>UGX {e["amount"]:,.0f}</td><td>{e["category"] or ""}</td><td>{e["expense_date"] if e["expense_date"] else ""}</td><td><a href="/expenses/edit/{e["id"]}" class="btn btn-small">Edit</a> <a href="/expenses/delete/{e["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for e in items) or '<tr><td colspan="5">No expenses.</td></tr>'
    return render_page("Expenses",f'<div class="card"><div class="card-header"><i class="fas fa-receipt"></i> Expenses</div><a href="/expenses/add" class="btn btn-success" style="margin-bottom:15px;">+ Add</a><table><tr><th>Description</th><th>Amount</th><th>Category</th><th>Date</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)
@app.route('/expenses/add', methods=['GET','POST'])
@login_required
def add_expense():
    if request.method == 'POST': db = get_db(); db.execute("INSERT INTO expenses (provider_id,description,amount,category,expense_date) VALUES (?,?,?,?,?)",(session['provider_id'],request.form['description'],float(request.form['amount']),request.form['category'],request.form['expense_date'])); db.commit(); return redirect('/expenses')
    return render_page("Add Expense",'<div class="card"><div class="card-header"><i class="fas fa-receipt"></i> Add Expense</div><form method="POST"><label>Description *</label><input type="text" name="description" required><label>Amount (UGX) *</label><input type="number" name="amount" step="0.01" required><label>Category</label><input type="text" name="category"><label>Date</label><input type="date" name="expense_date"><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>', get_pending_count(), admin=True)
@app.route('/expenses/edit/<int:eid>', methods=['GET','POST'])
@login_required
def edit_expense(eid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE expenses SET description=?,amount=?,category=?,expense_date=? WHERE id=? AND provider_id=?",(request.form['description'],float(request.form['amount']),request.form['category'],request.form['expense_date'],eid,session['provider_id'])); db.commit(); return redirect('/expenses')
    e = db.execute("SELECT * FROM expenses WHERE id=? AND provider_id=?",(eid,session['provider_id'])).fetchone()
    if not e: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Expense</div><form method="POST"><label>Description *</label><input type="text" name="description" value="{e["description"]}" required><label>Amount (UGX) *</label><input type="number" name="amount" step="0.01" value="{e["amount"]}" required><label>Category</label><input type="text" name="category" value="{e["category"] or ""}"><label>Date</label><input type="date" name="expense_date" value="{e["expense_date"] if e["expense_date"] else ""}"><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Expense", content, get_pending_count(), admin=True)
@app.route('/expenses/delete/<int:eid>')
@login_required
def delete_expense(eid): db = get_db(); db.execute("DELETE FROM expenses WHERE id=? AND provider_id=?",(eid,session['provider_id'])); db.commit(); return redirect('/expenses')

@app.route('/messages', methods=['GET','POST'])
@login_required
def messages():
    if request.method == 'POST': db = get_db(); db.execute("INSERT INTO notifications (user_id,type,message) VALUES (?,'admin_message',?)",(session['provider_id'],request.form['message'])); db.commit(); return redirect('/messages')
    db = get_db(); msgs = db.execute("SELECT message, created_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 20",(session['provider_id'],)).fetchall()
    rows = "".join(f'<tr><td>{m["message"]}</td><td>{m["created_at"][:16] if m["created_at"] else ""}</td></tr>' for m in msgs) or '<tr><td colspan="2">No messages sent.</td></tr>'
    return render_page("Messages",f'<div class="card"><div class="card-header"><i class="fas fa-envelope"></i> Send Message</div><form method="POST"><label>Message</label><textarea name="message" required></textarea><button type="submit" class="btn" style="margin-top:15px;">Send</button></form></div><div class="card"><div class="card-header">Sent Messages</div><table><tr><th>Message</th><th>Time</th></tr>{rows}</table></div>', get_pending_count(), admin=True)

@app.route('/email', methods=['GET','POST'])
@login_required
def email():
    if request.method == 'POST': content = '<div class="card"><div class="alert alert-success">Email sending feature coming soon.</div><a href="/email" class="btn">Back</a></div>'; return render_page("Email", content, get_pending_count(), admin=True)
    return render_page("Email",'<div class="card"><div class="card-header"><i class="fas fa-at"></i> Send Email</div><form method="POST"><label>To</label><input type="email" name="to" required><label>Subject</label><input type="text" name="subject" required><label>Body</label><textarea name="body"></textarea><button type="submit" class="btn" style="margin-top:15px;">Send</button></form><p style="color:var(--text-secondary);">SMTP not configured.</p></div>', get_pending_count(), admin=True)

@app.route('/campaign')
@login_required
def campaign():
    db = get_db(); items = db.execute("SELECT * FROM campaigns WHERE provider_id=? ORDER BY id DESC",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{c["name"]}</td><td>{c["description"] or ""}</td><td>{c["start_date"] if c["start_date"] else ""}</td><td>{c["end_date"] if c["end_date"] else ""}</td><td><a href="/campaign/edit/{c["id"]}" class="btn btn-small">Edit</a> <a href="/campaign/delete/{c["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for c in items) or '<tr><td colspan="5">No campaigns.</td></tr>'
    return render_page("Campaigns",f'<div class="card"><div class="card-header"><i class="fas fa-bullhorn"></i> Campaigns</div><a href="/campaign/add" class="btn btn-success" style="margin-bottom:15px;">+ Add</a><table><tr><th>Name</th><th>Description</th><th>Start</th><th>End</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)
@app.route('/campaign/add', methods=['GET','POST'])
@login_required
def add_campaign():
    if request.method == 'POST': db = get_db(); db.execute("INSERT INTO campaigns (provider_id,name,description,start_date,end_date) VALUES (?,?,?,?,?)",(session['provider_id'],request.form['name'],request.form['description'],request.form['start_date'],request.form['end_date'])); db.commit(); return redirect('/campaign')
    return render_page("Add Campaign",'<div class="card"><div class="card-header"><i class="fas fa-bullhorn"></i> Add Campaign</div><form method="POST"><label>Name *</label><input type="text" name="name" required><label>Description</label><textarea name="description"></textarea><label>Start Date</label><input type="date" name="start_date"><label>End Date</label><input type="date" name="end_date"><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>', get_pending_count(), admin=True)
@app.route('/campaign/edit/<int:cid>', methods=['GET','POST'])
@login_required
def edit_campaign(cid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE campaigns SET name=?,description=?,start_date=?,end_date=? WHERE id=? AND provider_id=?",(request.form['name'],request.form['description'],request.form['start_date'],request.form['end_date'],cid,session['provider_id'])); db.commit(); return redirect('/campaign')
    c = db.execute("SELECT * FROM campaigns WHERE id=? AND provider_id=?",(cid,session['provider_id'])).fetchone()
    if not c: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Campaign</div><form method="POST"><label>Name *</label><input type="text" name="name" value="{c["name"]}" required><label>Description</label><textarea name="description">{c["description"] or ""}</textarea><label>Start Date</label><input type="date" name="start_date" value="{c["start_date"] if c["start_date"] else ""}"><label>End Date</label><input type="date" name="end_date" value="{c["end_date"] if c["end_date"] else ""}"><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Campaign", content, get_pending_count(), admin=True)
@app.route('/campaign/delete/<int:cid>')
@login_required
def delete_campaign(cid): db = get_db(); db.execute("DELETE FROM campaigns WHERE id=? AND provider_id=?",(cid,session['provider_id'])); db.commit(); return redirect('/campaign')

@app.route('/equipment')
@login_required
def equipment():
    db = get_db(); items = db.execute("SELECT * FROM equipment WHERE provider_id=? ORDER BY id DESC",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{e["name"]}</td><td>{e["model"] or ""}</td><td>{e["serial_number"] or ""}</td><td>{e["status"]}</td><td><a href="/equipment/edit/{e["id"]}" class="btn btn-small">Edit</a> <a href="/equipment/delete/{e["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for e in items) or '<tr><td colspan="5">No equipment.</td></tr>'
    return render_page("Equipment",f'<div class="card"><div class="card-header"><i class="fas fa-tools"></i> Equipment</div><a href="/equipment/add" class="btn btn-success" style="margin-bottom:15px;">+ Add</a><table><tr><th>Name</th><th>Model</th><th>Serial</th><th>Status</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)
@app.route('/equipment/add', methods=['GET','POST'])
@login_required
def add_equipment():
    if request.method == 'POST': db = get_db(); db.execute("INSERT INTO equipment (provider_id,name,model,serial_number,status) VALUES (?,?,?,?,?)",(session['provider_id'],request.form['name'],request.form['model'],request.form['serial'],request.form['status'])); db.commit(); return redirect('/equipment')
    return render_page("Add Equipment",'<div class="card"><div class="card-header"><i class="fas fa-tools"></i> Add Equipment</div><form method="POST"><label>Name *</label><input type="text" name="name" required><label>Model</label><input type="text" name="model"><label>Serial Number</label><input type="text" name="serial"><label>Status</label><select name="status"><option value="active">Active</option><option value="inactive">Inactive</option></select><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>', get_pending_count(), admin=True)
@app.route('/equipment/edit/<int:eid>', methods=['GET','POST'])
@login_required
def edit_equipment(eid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE equipment SET name=?,model=?,serial_number=?,status=? WHERE id=? AND provider_id=?",(request.form['name'],request.form['model'],request.form['serial'],request.form['status'],eid,session['provider_id'])); db.commit(); return redirect('/equipment')
    eq = db.execute("SELECT * FROM equipment WHERE id=? AND provider_id=?",(eid,session['provider_id'])).fetchone()
    if not eq: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit Equipment</div><form method="POST"><label>Name *</label><input type="text" name="name" value="{eq["name"]}" required><label>Model</label><input type="text" name="model" value="{eq["model"] or ""}"><label>Serial Number</label><input type="text" name="serial" value="{eq["serial_number"] or ""}"><label>Status</label><select name="status"><option value="active" {"selected" if eq["status"]=="active" else ""}>Active</option><option value="inactive" {"selected" if eq["status"]=="inactive" else ""}>Inactive</option></select><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit Equipment", content, get_pending_count(), admin=True)
@app.route('/equipment/delete/<int:eid>')
@login_required
def delete_equipment(eid): db = get_db(); db.execute("DELETE FROM equipment WHERE id=? AND provider_id=?",(eid,session['provider_id'])); db.commit(); return redirect('/equipment')

@app.route('/mikrotik')
@login_required
def mikrotik():
    db = get_db(); routers = db.execute("SELECT * FROM mikrotik_routers WHERE provider_id=? ORDER BY id DESC",(session['provider_id'],)).fetchall()
    rows = ''.join(f'<tr><td>{r["name"]}</td><td>{r["ip_address"] or ""}</td><td>{r["username"] or ""}</td><td>{"Yes" if r["is_active"] else "No"}</td><td><a href="/mikrotik/edit/{r["id"]}" class="btn btn-small">Edit</a> <a href="/mikrotik/delete/{r["id"]}" class="btn btn-small btn-danger" onclick="return confirm(\'Delete?\')">Del</a></td></tr>' for r in routers) or '<tr><td colspan="5">No routers.</td></tr>'
    return render_page("MikroTik",f'<div class="card"><div class="card-header"><i class="fas fa-server"></i> MikroTik Routers</div><a href="/mikrotik/add" class="btn btn-success" style="margin-bottom:15px;">+ Add</a><table><tr><th>Name</th><th>IP</th><th>Username</th><th>Active</th><th>Action</th></tr>{rows}</table></div>', get_pending_count(), admin=True)
@app.route('/mikrotik/add', methods=['GET','POST'])
@login_required
def add_mikrotik():
    if request.method == 'POST': db = get_db(); db.execute("INSERT INTO mikrotik_routers (provider_id,name,ip_address,username,password,api_port,is_active) VALUES (?,?,?,?,?,?,?)",(session['provider_id'],request.form['name'],request.form['ip'],request.form['username'],request.form['password'],int(request.form['port'] or 8728),1 if request.form.get('is_active') else 0)); db.commit(); return redirect('/mikrotik')
    return render_page("Add MikroTik",'<div class="card"><div class="card-header"><i class="fas fa-server"></i> Add MikroTik Router</div><form method="POST"><label>Name *</label><input type="text" name="name" required><label>IP Address</label><input type="text" name="ip"><label>Username</label><input type="text" name="username"><label>Password</label><input type="password" name="password"><label>API Port</label><input type="number" name="port" value="8728"><label><input type="checkbox" name="is_active" checked> Active</label><button type="submit" class="btn" style="margin-top:20px;">Save</button></form></div>', get_pending_count(), admin=True)
@app.route('/mikrotik/edit/<int:rid>', methods=['GET','POST'])
@login_required
def edit_mikrotik(rid):
    db = get_db()
    if request.method == 'POST': db.execute("UPDATE mikrotik_routers SET name=?,ip_address=?,username=?,password=?,api_port=?,is_active=? WHERE id=? AND provider_id=?",(request.form['name'],request.form['ip'],request.form['username'],request.form['password'],int(request.form['port'] or 8728),1 if request.form.get('is_active') else 0,rid,session['provider_id'])); db.commit(); return redirect('/mikrotik')
    r = db.execute("SELECT * FROM mikrotik_routers WHERE id=? AND provider_id=?",(rid,session['provider_id'])).fetchone()
    if not r: return "Not found", 404
    content = f'<div class="card"><div class="card-header">Edit MikroTik Router</div><form method="POST"><label>Name *</label><input type="text" name="name" value="{r["name"]}" required><label>IP Address</label><input type="text" name="ip" value="{r["ip_address"] or ""}"><label>Username</label><input type="text" name="username" value="{r["username"] or ""}"><label>Password</label><input type="password" name="password" value="{r["password"] or ""}"><label>API Port</label><input type="number" name="port" value="{r["api_port"] or 8728}"><label><input type="checkbox" name="is_active" {"checked" if r["is_active"] else ""}> Active</label><button type="submit" class="btn" style="margin-top:20px;">Update</button></form></div>'
    return render_page("Edit MikroTik", content, get_pending_count(), admin=True)
@app.route('/mikrotik/delete/<int:rid>')
@login_required
def delete_mikrotik(rid): db = get_db(); db.execute("DELETE FROM mikrotik_routers WHERE id=? AND provider_id=?",(rid,session['provider_id'])); db.commit(); return redirect('/mikrotik')

# ------------------------------------------------------------
init_db()
if __name__ == '__main__':
    app.run()
