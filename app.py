import os, sqlite3, re, random, string
from datetime import date, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

app = Flask(__name__)
app.secret_key = 'rockabywifi-secret-key-change-in-production'
app.permanent_session_lifetime = timedelta(days=30)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ------------------------------------------------------------
# DATABASE (fixed migration)
# ------------------------------------------------------------
def init_db():
    conn = sqlite3.connect('rockabywifi.db')
    conn.execute("PRAGMA busy_timeout = 5000;")
    c = conn.cursor()

    # Providers
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

    # Safe column migration
    c.execute("PRAGMA table_info(providers)")
    existing_cols = [col[1] for col in c.fetchall()]
    for col in ['poster_image', 'logo_image', 'support_phone']:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE providers ADD COLUMN {col} TEXT")

    # Plans
    c.execute('''CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        price_ugx INTEGER NOT NULL,
        is_active INTEGER DEFAULT 1,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # Voucher requests
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

    # Vouchers
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

    # Subscribers
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

    # Sessions
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher_id INTEGER,
        subscriber_id INTEGER,
        provider_id INTEGER,
        mac_address TEXT,
        ip_address TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP,
        FOREIGN KEY(voucher_id) REFERENCES vouchers(id),
        FOREIGN KEY(subscriber_id) REFERENCES subscribers(id)
    )''')

    # Restricted
    c.execute('''CREATE TABLE IF NOT EXISTS restricted (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER,
        phone_number TEXT,
        mac_address TEXT,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Settings
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # Default admin
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
# HELPERS (unchanged)
# ------------------------------------------------------------
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
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT id, name, duration_minutes, price_ugx FROM plans WHERE provider_id=? AND is_active=1", (provider_id,))
    plans = c.fetchall()
    conn.close()
    return ''.join(f'<option value="{p[0]}">{p[1]} - {p[2]} min - UGX {p[3]:,}</option>' for p in plans)

def get_pending_count():
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM voucher_requests WHERE provider_id=1 AND status='pending'")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_auto_approve():
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT auto_approve FROM providers WHERE id=1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else 1

def get_provider(provider_id):
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT * FROM providers WHERE id=?", (provider_id,))
    row = c.fetchone()
    conn.close()
    return row

def clean_number(num):
    digits = ''.join(filter(str.isdigit, num))
    if digits.startswith('0'): digits = '256' + digits[1:]
    elif not digits.startswith('256'): digits = '256' + digits
    return digits

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_weekly_platform_revenue():
    today = date.today()
    start_of_week = today if today.weekday() == 6 else today - timedelta(days=today.weekday() + 1)
    end_of_week = start_of_week + timedelta(days=6)
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT COALESCE(SUM(pl.price_ugx), 0) FROM vouchers v JOIN plans pl ON v.plan_id=pl.id WHERE v.provider_id=1 AND date(v.created_at) BETWEEN ? AND ?", (start_of_week.isoformat(), end_of_week.isoformat()))
    total = c.fetchone()[0]
    conn.close()
    return int(total * 0.05), start_of_week, end_of_week

# ------------------------------------------------------------
# BASE TEMPLATE (clean sidebar with headings, settings in topbar)
# ------------------------------------------------------------
base_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RockabyWiFi - {title}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    {% raw %}
    <style>
        :root {
            --primary: #1a73e8; --primary-dark: #1557b0;
            --bg: #f0f4f8; --card-bg: #ffffff; --text: #1a1a1a;
            --text-secondary: #666666; --border: #e0e0e0;
            --radius: 12px; --shadow: 0 1px 3px rgba(0,0,0,0.1);
            --sidebar-width: 250px;
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg); color: var(--text); min-height: 100vh;
        }
        .admin-layout { display: flex; }
        .sidebar {
            width: var(--sidebar-width);
            background: #1e293b;
            color: #fff;
            height: 100vh;
            position: fixed;
            left: 0; top: 0;
            overflow-y: auto;
            transition: transform 0.3s;
            z-index: 1000;
        }
        .sidebar.collapsed { transform: translateX(-100%); }
        .sidebar-header {
            padding: 20px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            display: flex; align-items: center; gap: 10px;
        }
        .sidebar-header img { height: 36px; width: 36px; border-radius: 8px; }
        .sidebar-header h3 { font-size: 1.1rem; font-weight: 600; }
        .sidebar-menu { padding: 10px 0; }
        .sidebar-menu .menu-heading {
            padding: 12px 20px 5px;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #94a3b8;
        }
        .sidebar-menu a {
            display: flex; align-items: center; gap: 10px;
            padding: 10px 20px; color: #cbd5e1; text-decoration: none;
            transition: background 0.2s; font-size: 0.9rem;
        }
        .sidebar-menu a:hover, .sidebar-menu a.active { background: rgba(255,255,255,0.1); color: #fff; }
        .sidebar-footer {
            position: absolute; bottom: 0; width: 100%;
            padding: 10px; border-top: 1px solid rgba(255,255,255,0.1);
            text-align: center;
        }
        .sidebar-footer a { color: #cbd5e1; text-decoration: none; font-size: 0.9rem; }
        .main-content {
            margin-left: var(--sidebar-width);
            flex: 1; transition: margin-left 0.3s;
        }
        .main-content.expanded { margin-left: 0; }
        .topbar {
            background: var(--card-bg); padding: 12px 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            display: flex; align-items: center; justify-content: space-between;
        }
        .hamburger { font-size: 1.5rem; cursor: pointer; background: none; border: none; color: var(--text); }
        .topbar-right { display: flex; align-items: center; gap: 15px; }
        .topbar-right a { color: var(--text-secondary); text-decoration: none; }
        .container { max-width: 900px; margin: 20px auto; padding: 0 15px; }
        .card {
            background: var(--card-bg); border-radius: var(--radius); padding: 24px;
            margin-bottom: 16px; box-shadow: var(--shadow); border: 1px solid var(--border);
        }
        .card-header { font-size: 1.2rem; font-weight: 600; margin-bottom: 15px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
        label { display: block; margin-top: 15px; font-weight: 500; }
        input, textarea, select {
            width: 100%; padding: 10px 12px; margin-top: 5px; border-radius: 6px;
            border: 1px solid var(--border); font-size: 0.95rem;
        }
        .btn {
            display: inline-block; padding: 10px 20px; background: var(--primary);
            color: #fff; border: none; border-radius: 6px; font-weight: 600;
            cursor: pointer; text-decoration: none; font-size: 0.9rem;
        }
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
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
        .voucher-code {
            font-size: 1.5rem; font-weight: 700; letter-spacing: 1px;
            background: #f0f4f8; padding: 10px 15px; border-radius: 8px;
            display: inline-block; margin: 10px 0;
        }
        .whatsapp-float {
            position: fixed; bottom: 20px; right: 20px;
            background: #25D366; color: white; width: 60px; height: 60px;
            border-radius: 50%; display: flex; align-items: center; justify-content: center;
            font-size: 30px; box-shadow: 0 4px 10px rgba(0,0,0,0.3);
            z-index: 999; text-decoration: none;
        }
        .dropdown { position: relative; display: inline-block; }
        .dropdown-content {
            display: none; position: absolute; background: white; min-width: 200px;
            box-shadow: 0 8px 16px rgba(0,0,0,0.2); z-index: 1; right:0;
        }
        .dropdown-content a { color: black; padding: 8px 12px; text-decoration: none; display: block; }
        .dropdown-content a:hover { background: #f1f1f1; }
        .dropdown:hover .dropdown-content { display: block; }
        @media (max-width: 768px) {
            .sidebar { transform: translateX(-100%); }
            .sidebar.open { transform: translateX(0); }
            .main-content { margin-left: 0; }
        }
    </style>
    {% endraw %}
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
        function toggleSidebar() {{
            document.getElementById('sidebar').classList.toggle('collapsed');
            document.getElementById('mainContent').classList.toggle('expanded');
        }}
    </script>
</body>
</html>
"""

def render_page(title, content, pending_count=0, provider_id=1, admin=False):
    provider = get_provider(provider_id)
    support_phone = provider[14] if provider and len(provider) > 14 and provider[14] else '256751318876'

    # Admin sidebar with headings and no @
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
                <a href="#"><i class="fas fa-ticket-alt"></i> Tickets</a>
                <a href="#"><i class="fas fa-chart-line"></i> Leads</a>
                <div class="menu-heading">FINANCE</div>
                <a href="/plans"><i class="fas fa-box"></i> Packages</a>
                <a href="/pending"><i class="fas fa-money-bill-wave"></i> Payments</a>
                <a href="/generate-cash"><i class="fas fa-ticket-alt"></i> Vouchers</a>
                <a href="#"><i class="fas fa-receipt"></i> Expenses</a>
                <div class="menu-heading">COMMUNICATION</div>
                <a href="#"><i class="fas fa-envelope"></i> Messages</a>
                <a href="#"><i class="fas fa-at"></i> Email</a>
                <a href="#"><i class="fas fa-bullhorn"></i> Campaign</a>
                <div class="menu-heading">DEVICES</div>
                <a href="/mikrotik"><i class="fas fa-server"></i> MikroTik</a>
                <a href="#"><i class="fas fa-tools"></i> Equipment</a>
            </div>
            <div class="sidebar-footer">
                <a href="/logout"><i class="fas fa-sign-out-alt"></i> Logout</a>
            </div>
        </div>
        """
        topbar_html = f'''<div class="topbar">
            <button class="hamburger" onclick="toggleSidebar()">&#9776;</button>
            <div class="topbar-right">
                <span>Welcome, {session['provider_name']}</span>
                <a href="/provider/edit" title="Settings"><i class="fas fa-cog"></i></a>
            </div>
        </div>'''
        layout_class = 'admin-layout'
    else:
        sidebar_html = ''
        topbar_html = '<div class="topbar" style="background:transparent; box-shadow:none;"></div>'
        layout_class = 'public-layout'

    page = base_template.replace('{title}', title)
    page = page.replace('{layout_class}', layout_class)
    page = page.replace('{sidebar_html}', sidebar_html)
    page = page.replace('{topbar_html}', topbar_html)
    page = page.replace('{content}', content)
    page = page.replace('{support_phone}', support_phone)
    return page

# ------------------------------------------------------------
# CUSTOMER ROUTES (unchanged)
# ------------------------------------------------------------
@app.route('/')
def home():
    provider = get_provider(1)
    business_name = provider[1] if provider else 'RockabyWiFi'
    logo_html = f'<img src="/static/uploads/{provider[13]}" style="height:50px; width:50px; border-radius:10px; margin-right:12px; vertical-align:middle; object-fit:cover; border:2px solid var(--primary);" alt="{business_name}">' if provider and len(provider) > 13 and provider[13] else ''
    poster_html = f'<img src="/static/uploads/{provider[11]}" style="width:100%; max-height:220px; object-fit:cover; border-radius:12px; margin-bottom:15px; box-shadow:0 1px 3px rgba(0,0,0,0.1);" alt="Poster">' if provider and provider[11] else ''
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

@app.route('/subscriber-login', methods=['GET', 'POST'])
def subscriber_login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("SELECT id, password_hash, suspended FROM subscribers WHERE username=? AND provider_id=1", (username,))
        sub = c.fetchone()
        if sub and check_password_hash(sub[1], password) and not sub[2]:
            c.execute("DELETE FROM sessions WHERE subscriber_id=?", (sub[0],))
            ip = request.remote_addr
            c.execute("INSERT INTO sessions (subscriber_id, provider_id, ip_address) VALUES (?, 1, ?)", (sub[0], ip))
            c.execute("UPDATE subscribers SET current_ip=? WHERE id=?", (ip, sub[0]))
            conn.commit()
            conn.close()
            session['subscriber_id'] = sub[0]
            session['subscriber_name'] = username
            return redirect(url_for('subscriber_portal'))
        else:
            conn.close()
            return render_page("Subscriber Login", '<div class="card"><div class="alert alert-error">Invalid credentials or account suspended.</div><a href="/subscriber-login" class="btn">Try again</a></div>', get_pending_count(), admin=False)
    return render_page("Subscriber Login", '<div class="card"><div class="card-header">Subscriber Login</div><form method="POST"><label>Username</label><input type="text" name="username" required><label>Password</label><input type="password" name="password" required><button type="submit" class="btn" style="margin-top:20px;">Login</button></form></div>', get_pending_count(), admin=False)

@app.route('/subscriber-portal')
def subscriber_portal():
    if 'subscriber_id' not in session:
        return redirect('/subscriber-login')
    content = f"""
        <div class="card">
            <h2>Welcome, {session['subscriber_name']}</h2>
            <p>You are connected. Your IP: {request.remote_addr}</p>
            <a href="/subscriber-logout" class="btn btn-danger">Logout / Switch Device</a>
        </div>
    """
    return render_page("Subscriber Portal", content, get_pending_count(), admin=False)

@app.route('/subscriber-logout')
def subscriber_logout():
    if 'subscriber_id' in session:
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("DELETE FROM sessions WHERE subscriber_id=?", (session['subscriber_id'],))
        conn.commit()
        conn.close()
        session.pop('subscriber_id', None)
        session.pop('subscriber_name', None)
    return redirect('/')

# (Other public routes: /redeem, /sms-verify are identical to previous working versions – include them here as before)
# ------------------------------------------------------------
# ADMIN ROUTES (with topbar settings icon)
# ------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        contact = request.form['contact'].strip()
        password = request.form['password']
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("SELECT id, business_name, password_hash, is_active FROM providers WHERE contact=?", (contact,))
        provider = c.fetchone()
        conn.close()
        if provider and check_password_hash(provider[2], password) and provider[3]:
            session['provider_id'] = provider[0]
            session['provider_name'] = provider[1]
            if request.form.get('remember'):
                session.permanent = True
            return redirect('/dashboard')
        return render_page("Admin Login", '<div class="card"><div class="alert alert-error">Invalid credentials.</div><p><a href="/login">Try again</a></p></div>', 0, admin=False)
    return render_page("Admin Login", '<div class="card"><div class="card-header">Provider Login</div><form method="POST"><label>Phone Number</label><input type="tel" name="contact" required><label>Password</label><input type="password" name="password" required><label><input type="checkbox" name="remember"> Remember me</label><button type="submit" class="btn" style="margin-top:20px; width:100%;">Login</button></form></div>', 0, admin=False)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

@app.route('/dashboard')
@login_required
def dashboard():
    provider_id = session['provider_id']
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at)=?", (provider_id, today))
    sms_count, sms_rev = c.fetchone()
    c.execute("SELECT COUNT(*), COALESCE(SUM(plans.price_ugx),0) FROM vouchers v JOIN plans ON v.plan_id=plans.id WHERE v.provider_id=? AND v.payment_method='cash' AND date(v.created_at)=?", (provider_id, today))
    cash_count, cash_rev = c.fetchone()
    pending = get_pending_count()
    auto_approve = get_auto_approve()
    auto_status = "ON" if auto_approve else "OFF"
    auto_color = "#28a745" if auto_approve else "#dc3545"
    weekly_fee, week_start, week_end = get_weekly_platform_revenue()
    conn.close()
    content = f"""
        <div class="card"><h2>Welcome, {session['provider_name']}</h2>
        <div style="display:flex; align-items:center; gap:15px; margin-top:15px;"><p><strong>Auto-Approval:</strong> <span style="color:{auto_color}; font-weight:700;">{auto_status}</span></p>
        <a href="/toggle-auto" class="btn btn-small" style="background:{'#dc3545' if auto_approve else '#28a745'};">Turn {'OFF' if auto_approve else 'ON'}</a></div></div>
        <div class="stat-grid">
        <div class="card" style="text-align:center;"><h3>UGX {sms_rev or 0:,}</h3><small>SMS Revenue Today</small></div>
        <div class="card" style="text-align:center;"><h3>UGX {cash_rev or 0:,}</h3><small>Cash Revenue Today</small></div>
        <div class="card" style="text-align:center;"><h3>{pending}</h3><small>Pending Approvals</small></div></div>
        <div class="platform-revenue"><strong>RockabyTech Platform Fee (5% this week):</strong> UGX {weekly_fee:,} &nbsp; <small>({week_start.strftime('%d %b')} - {week_end.strftime('%d %b')})</small></div>
    """
    return render_page("Dashboard", content, pending, provider_id, admin=True)

# (All other admin routes – /active-users, /subscribers, /plans, /pending, /approve, /reject, /generate-cash, /stats, /toggle-auto, /provider/edit, /mikrotik, etc. – remain exactly as in the previous full version)
# ...
init_db()
if __name__ == '__main__':
    app.run()
