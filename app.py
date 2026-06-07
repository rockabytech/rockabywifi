import os, sqlite3, re, random, string
from datetime import date, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

app = Flask(__name__)
app.secret_key = 'rockabywifi-secret-key-change-in-production'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------
def init_db():
    conn = sqlite3.connect('rockabywifi.db')
    conn.execute("PRAGMA busy_timeout = 5000;")
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

    # Add missing columns safely
    c.execute("PRAGMA table_info(providers)")
    prov_cols = [col[1] for col in c.fetchall()]
    if 'poster_image' not in prov_cols:
        c.execute("ALTER TABLE providers ADD COLUMN poster_image TEXT")
    if 'logo_image' not in prov_cols:
        c.execute("ALTER TABLE providers ADD COLUMN logo_image TEXT")
    if 'support_phone' not in prov_cols:
        c.execute("ALTER TABLE providers ADD COLUMN support_phone TEXT")

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

    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voucher_id INTEGER,
        provider_id INTEGER,
        mac_address TEXT,
        ip_address TEXT,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP,
        FOREIGN KEY(voucher_id) REFERENCES vouchers(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS blacklist (
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

    # Default provider
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
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'provider_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def parse_mtn_sms(sms):
    tid = re.search(r'ID:\s*(\d+)', sms)
    amount = re.search(r'UGX\s*([\d,]+)', sms)
    recipient_name = re.search(r'to\s+(.+?),', sms)
    number_match = re.search(r'to\s+.+?[, ]+(\d{10,12})', sms)
    date_str = re.search(r'on\s+(\d{4}-\d{2}-\d{2})', sms)
    return {
        'tid': tid.group(1) if tid else None,
        'amount': int(amount.group(1).replace(',','')) if amount else None,
        'recipient_name': recipient_name.group(1).strip() if recipient_name else None,
        'recipient_number': number_match.group(1) if number_match else None,
        'date': date_str.group(1) if date_str else None
    }

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
    return {
        'tid': tid.group(1) if tid else None,
        'amount': int(amount.group(1).replace(',','')) if amount else None,
        'recipient_name': recipient_name,
        'recipient_number': recipient_number,
        'date': date_str.group(1) if date_str else None
    }

def generate_voucher_code():
    chars = string.ascii_uppercase + string.digits
    return 'WIFI-' + ''.join(random.choices(chars, k=4)) + '-' + ''.join(random.choices(chars, k=4)) + '-' + ''.join(random.choices(chars, k=4))

def get_plan_options(provider_id):
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT id, name, duration_minutes, price_ugx FROM plans WHERE provider_id=? AND is_active=1", (provider_id,))
    plans = c.fetchall()
    conn.close()
    opts = ""
    for p in plans:
        opts += f'<option value="{p[0]}">{p[1]} - {p[2]} min - UGX {p[3]:,}</option>'
    return opts

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
    if digits.startswith('0'):
        digits = '256' + digits[1:]
    elif not digits.startswith('256'):
        digits = '256' + digits
    return digits

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_weekly_platform_revenue():
    today = date.today()
    if today.weekday() == 6:
        start_of_week = today
    else:
        start_of_week = today - timedelta(days=today.weekday() + 1)
    end_of_week = start_of_week + timedelta(days=6)
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("""SELECT COALESCE(SUM(pl.price_ugx), 0) FROM vouchers v
                 JOIN plans pl ON v.plan_id = pl.id
                 WHERE v.provider_id = 1 AND date(v.created_at) BETWEEN ? AND ?""",
              (start_of_week.isoformat(), end_of_week.isoformat()))
    total = c.fetchone()[0]
    conn.close()
    return int(total * 0.05), start_of_week, end_of_week

# ------------------------------------------------------------
# BASE TEMPLATE (ROCKABYTECH branded)
# ------------------------------------------------------------
base_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RockabyWiFi - {title}</title>
    {% raw %}
    <style>
        :root {
            --primary: #1a73e8;
            --primary-dark: #1557b0;
            --bg: #f0f4f8;
            --card-bg: #ffffff;
            --text: #1a1a1a;
            --text-secondary: #666666;
            --border: #e0e0e0;
            --radius: 12px;
            --shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
        }
        .navbar {
            background: var(--card-bg);
            box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            padding: 12px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
        }
        .navbar .logo {
            font-size: 1.3rem;
            font-weight: 700;
            color: var(--primary);
            text-decoration: none;
        }
        .nav-links {
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            align-items: center;
        }
        .nav-links a {
            color: var(--text-secondary);
            text-decoration: none;
            font-weight: 500;
            font-size: 0.9rem;
        }
        .nav-links a:hover { color: var(--primary); }
        .btn {
            display: inline-block;
            padding: 10px 20px;
            background: var(--primary);
            color: #fff;
            border: none;
            border-radius: 6px;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
            font-size: 0.9rem;
        }
        .btn:hover { background: var(--primary-dark); }
        .btn-outline {
            background: transparent;
            border: 1px solid var(--primary);
            color: var(--primary);
        }
        .btn-small { padding: 5px 10px; font-size: 0.8rem; }
        .btn-danger { background: #dc3545; }
        .btn-success { background: #28a745; }
        .container { max-width: 700px; margin: 20px auto; padding: 0 15px; }
        .card {
            background: var(--card-bg);
            border-radius: var(--radius);
            padding: 24px;
            margin-bottom: 16px;
            box-shadow: var(--shadow);
            border: 1px solid var(--border);
        }
        .card-header {
            font-size: 1.2rem;
            font-weight: 600;
            margin-bottom: 15px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 10px;
        }
        label { display: block; margin-top: 15px; font-weight: 500; }
        input, textarea, select {
            width: 100%;
            padding: 10px 12px;
            margin-top: 5px;
            border-radius: 6px;
            border: 1px solid var(--border);
            font-size: 0.95rem;
        }
        .alert { padding: 10px 15px; border-radius: 6px; margin-bottom: 15px; }
        .alert-success { background: #d4edda; color: #155724; }
        .alert-error { background: #f8d7da; color: #721c24; }
        footer { text-align: center; color: var(--text-secondary); padding: 30px 0; font-size: 0.9rem; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 8px; text-align: left; border-bottom: 1px solid var(--border); }
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
        .voucher-code {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: 1px;
            background: #f0f4f8;
            padding: 10px 15px;
            border-radius: 8px;
            display: inline-block;
            margin: 10px 0;
        }
        .platform-revenue {
            background: #e8f0fe;
            border-left: 4px solid var(--primary);
            padding: 10px 15px;
            margin: 10px 0;
            border-radius: 6px;
        }
        .copy-btn {
            background: #28a745;
            color: white;
            border: none;
            padding: 8px 15px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
            margin-left: 10px;
        }
        .copy-btn:hover { background: #218838; }
        .whatsapp-float {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #25D366;
            color: white;
            width: 60px;
            height: 60px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 30px;
            box-shadow: 0 4px 10px rgba(0,0,0,0.3);
            z-index: 999;
            text-decoration: none;
        }
        .provider-logo {
            height: 50px;
            width: 50px;
            border-radius: 10px;
            margin-right: 12px;
            vertical-align: middle;
            object-fit: cover;
            border: 2px solid var(--primary);
        }
        .provider-poster {
            width: 100%;
            max-height: 220px;
            object-fit: cover;
            border-radius: var(--radius);
            margin-bottom: 15px;
            box-shadow: var(--shadow);
        }
        @media (max-width: 600px) {
            .navbar { flex-direction: column; gap: 10px; }
        }
    </style>
    {% endraw %}
</head>
<body>
<nav class="navbar">
    <a href="/" class="logo" style="display:flex; align-items:center; gap:8px;">
        <img src="/static/icon-192.png" alt="RockabyTech" style="height:32px; width:32px; border-radius:6px;">
        ROCKABY<span style="color:var(--primary-dark);">TECH</span>
    </a>
    <div class="nav-links">
        {nav_links}
    </div>
</nav>
    <div class="container">
        {content}
    </div>
    <footer>
        &copy; 2025 RockabyTech - WiFi Billing Made Simple
    </footer>
    <a href="https://wa.me/{support_phone}?text=Hi%20RockabyWiFi%20Support" target="_blank" class="whatsapp-float">💬</a>
</body>
</html>
"""

def render_page(title, content, pending_count=0, provider_id=1):
    provider = get_provider(provider_id)
    support_phone = provider[14] if provider and len(provider) > 14 and provider[14] else '256751318876'
    if session.get('provider_id'):
        nav = f'<a href="/dashboard">Dashboard</a> <a href="/pending">Pending ({pending_count})</a> <a href="/generate-cash">Cash Voucher</a> <a href="/plans">Plans</a> <a href="/provider/edit">Settings</a> <a href="/logout">Logout</a>'
    else:
        nav = ''
    page = base_template.replace('{title}', title)
    page = page.replace('{nav_links}', nav)
    page = page.replace('{content}', content)
    page = page.replace('{support_phone}', support_phone)
    return page

# ------------------------------------------------------------
# CUSTOMER ROUTES
# ------------------------------------------------------------
@app.route('/')
def home():
    provider = get_provider(1)
    business_name = provider[1] if provider else 'RockabyWiFi'
    logo_html = ''
    poster_html = ''
    if provider:
        # Logo (index 13)
        if len(provider) > 13 and provider[13]:
            logo_html = f'<img src="/static/uploads/{provider[13]}" class="provider-logo" alt="{business_name}">'
        # Poster (index 11)
        if provider[11]:
            poster_html = f'<img src="/static/uploads/{provider[11]}" class="provider-poster" alt="Poster">'

    content = f"""
        <div class="card" style="display:flex; align-items:center;">
            {logo_html}
            <h2 style="margin:0;">{business_name}</h2>
        </div>
        {poster_html}
        <div class="card">
            <div class="card-header">Choose a Plan</div>
            <form method="GET" action="/sms-verify">
                <label>Your Phone Number *</label>
                <input type="tel" name="phone" required>
                <label>Select Plan</label>
                <select name="plan_id" required>
                    {get_plan_options(1)}
                </select>
                <button type="submit" class="btn" style="margin-top:20px; width:100%;">Continue to Payment</button>
            </form>
        </div>
        <p style="text-align:center; margin-top:15px;">
            <a href="/redeem" class="btn btn-outline">Already have a voucher? Enter it here</a>
        </p>
    """
    return render_page("Get Internet Access", content, get_pending_count())

# ---- (All remaining routes: /redeem, /sms-verify, /login, /dashboard, /toggle-auto, /plans, /provider/edit, /pending, /approve, /reject, /generate-cash, /stats are identical to the previous full code) ----
# ---- Include them here exactly as they were in the last complete file ----

# [PASTE THE REST OF THE ROUTES FROM THE PREVIOUS COMPLETE CODE HERE]

init_db()
if __name__ == '__main__':
    app.run()
