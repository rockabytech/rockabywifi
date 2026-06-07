import os, sqlite3, re, random, string
from datetime import date, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

app = Flask(__name__)
app.secret_key = 'rockabywifi-secret-key-change-in-production'

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
        support_phone TEXT
    )''')

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

    # Default provider (RockabyWiFi Admin)
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
    c.execute("""
        SELECT COALESCE(SUM(pl.price_ugx), 0)
        FROM vouchers v
        JOIN plans pl ON v.plan_id = pl.id
        WHERE v.provider_id = 1
          AND date(v.created_at) BETWEEN ? AND ?
    """, (start_of_week.isoformat(), end_of_week.isoformat()))
    total = c.fetchone()[0]
    conn.close()
    return int(total * 0.05), start_of_week, end_of_week

# ------------------------------------------------------------
# BASE TEMPLATE (no public admin link)
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
        @media (max-width: 600px) {
            .navbar { flex-direction: column; gap: 10px; }
        }
    </style>
    {% endraw %}
</head>
<body>
    <nav class="navbar">
        <a href="/" class="logo">&#x1F4E1; ROCKABYWIFI</a>
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
    """Render a page. Provider-specific: support phone and nav."""
    provider = get_provider(provider_id)
    support_phone = provider[12] if provider and provider[12] else '256751318876'   # default support
    if session.get('provider_id'):
        nav = f'<a href="/dashboard">Dashboard</a> <a href="/pending">Pending ({pending_count})</a> <a href="/generate-cash">Cash Voucher</a> <a href="/plans">Plans</a> <a href="/provider/edit">Settings</a> <a href="/logout">Logout</a>'
    else:
        nav = ''   # no admin links for public
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
    poster_html = ''
    if provider and provider[11]:
        poster_html = f'<img src="/static/uploads/{provider[11]}" style="width:100%; max-height:200px; object-fit:cover; border-radius:8px; margin-bottom:15px;">'
    content = f"""
        {poster_html}
        <div class="card">
            <h2>Get Internet Access</h2>
            <p style="color:#666;">Select a plan, pay via Mobile Money, paste your SMS, and get your voucher instantly.</p>
        </div>
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

@app.route('/redeem', methods=['GET', 'POST'])
def redeem():
    if request.method == 'POST':
        code = request.form['code'].strip().upper()
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("SELECT id, used FROM vouchers WHERE code=?", (code,))
        voucher = c.fetchone()
        if voucher and not voucher[1]:
            c.execute("UPDATE vouchers SET used=1, used_at=CURRENT_TIMESTAMP WHERE id=?", (voucher[0],))
            conn.commit()
            conn.close()
            content = f"""
                <div class="card">
                    <div class="alert alert-success">Connected! Enjoy your internet access.</div>
                    <a href="/" class="btn">Back to Home</a>
                </div>
            """
            return render_page("Voucher Redeemed", content, get_pending_count())
        else:
            conn.close()
            error = "Invalid or already used voucher code."
            content = f"""
                <div class="card">
                    <div class="alert alert-error">{error}</div>
                    <form method="POST">
                        <label>Enter Voucher Code</label>
                        <input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required>
                        <button type="submit" class="btn" style="margin-top:15px; width:100%;">Redeem</button>
                    </form>
                </div>
            """
            return render_page("Redeem Voucher", content, get_pending_count())

    content = """
        <div class="card">
            <div class="card-header">Redeem Voucher</div>
            <form method="POST">
                <label>Enter Voucher Code</label>
                <input type="text" name="code" placeholder="WIFI-XXXX-XXXX-XXXX" required>
                <button type="submit" class="btn" style="margin-top:15px; width:100%;">Redeem</button>
            </form>
        </div>
    """
    return render_page("Redeem Voucher", content, get_pending_count())

@app.route('/sms-verify', methods=['GET', 'POST'])
def sms_verify():
    phone = request.args.get('phone', '')
    plan_id = request.args.get('plan_id', '1')
    pending_count = get_pending_count()

    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT id, name, duration_minutes, price_ugx FROM plans WHERE id=?", (plan_id,))
    plan = c.fetchone()
    if not plan:
        conn.close()
        return "Invalid plan selected.", 400

    c.execute("SELECT auto_approve, mtn_number, airtel_number FROM providers WHERE id=1")
    provider = c.fetchone()
    conn.close()

    if request.method == 'POST':
        phone = request.form['phone'].strip()
        plan_id = int(request.form['plan_id'])
        raw_sms = request.form['raw_sms'].strip()

        if 'TID' in raw_sms or 'SENT.TID' in raw_sms:
            parsed = parse_airtel_sms(raw_sms)
        else:
            parsed = parse_mtn_sms(raw_sms)

        error = None
        if not parsed['tid']:
            error = "Could not detect Transaction ID."
        elif not parsed['amount']:
            error = "Could not detect amount."
        elif parsed['amount'] != plan[3]:
            error = f"Amount mismatch. Expected UGX {plan[3]:,}."
        elif not parsed.get('recipient_name'):
            error = "Could not detect recipient."
        else:
            mtn_num = clean_number(provider[1]) if provider[1] else ''
            airtel_num = clean_number(provider[2]) if provider[2] else ''
            sms_num = clean_number(parsed.get('recipient_number', '')) if parsed.get('recipient_number') else ''
            if sms_num:
                if sms_num != mtn_num and sms_num != airtel_num:
                    error = "Payment not sent to the correct provider number."
            else:
                recipient_lower = parsed['recipient_name'].lower()
                if provider[1] and provider[1] not in recipient_lower and provider[2] and provider[2] not in recipient_lower:
                    error = "Payment not sent to the correct provider number."

        if error:
            content = f"""
                <div class="card">
                    <div class="alert alert-error">{error}</div>
                    <form method="POST">
                        <input type="hidden" name="phone" value="{phone}">
                        <input type="hidden" name="plan_id" value="{plan_id}">
                        <label>Paste Full MTN/Airtel SMS Here</label>
                        <textarea name="raw_sms" rows="6" required></textarea>
                        <button type="submit" class="btn" style="margin-top:20px; width:100%;">Verify Payment</button>
                    </form>
                </div>
            """
            return render_page("Verify Payment", content, pending_count)

        # Duplicate check
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM voucher_requests WHERE transaction_id=?", (parsed['tid'],))
        if c.fetchone()[0] > 0:
            conn.close()
            content = """
                <div class="card">
                    <div class="alert alert-error">This Transaction ID has already been used.</div>
                    <p><a href="/" class="btn">Back to Home</a></p>
                </div>
            """
            return render_page("Verify Payment", content, pending_count)
        conn.close()

        auto_approve = provider[0] if provider else 1
        status = 'approved' if auto_approve else 'pending'
        voucher_code = None
        recipient_full = f"{parsed.get('recipient_name','')} {parsed.get('recipient_number','')}".strip()

        if status == 'approved':
            voucher_code = generate_voucher_code()
            conn = sqlite3.connect('rockabywifi.db')
            c = conn.cursor()
            c.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number) VALUES (1, ?, ?, 'sms', ?)",
                      (voucher_code, plan_id, phone))
            c.execute("""INSERT INTO voucher_requests (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status, voucher_code)
                         VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'approved', ?)""",
                      (phone, plan_id, raw_sms, parsed['tid'], parsed['amount'], recipient_full, parsed['date'], voucher_code))
            conn.commit()
            conn.close()

            content = f"""
                <div class="card">
                    <div class="alert alert-success">Payment verified!</div>
                    <p><strong>Your Voucher Code:</strong></p>
                    <div class="voucher-code" id="voucherCode">{voucher_code}</div>
                    <button class="copy-btn" onclick="copyVoucher()">📋 Copy</button>
                    <p style="margin-top:10px;">Use this code on the <a href="/redeem">Redeem page</a> to connect.</p>
                    <a href="/" class="btn">Back to Home</a>
                </div>
                <script>
                    function copyVoucher() {{
                        const code = document.getElementById('voucherCode').innerText;
                        navigator.clipboard.writeText(code).then(() => alert('Copied!'));
                    }}
                </script>
            """
        else:
            conn = sqlite3.connect('rockabywifi.db')
            c = conn.cursor()
            c.execute("""INSERT INTO voucher_requests (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status)
                         VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                      (phone, plan_id, raw_sms, parsed['tid'], parsed['amount'], recipient_full, parsed['date']))
            conn.commit()
            conn.close()

            content = """
                <div class="card">
                    <div class="alert alert-success">Payment submitted! Waiting for approval.</div>
                    <p><a href="/" class="btn">Back to Home</a></p>
                </div>
            """

        return render_page("Verification Result", content, get_pending_count())

    content = f"""
        <div class="card">
            <div class="card-header">Pay for Internet</div>
            <p><strong>Selected Plan:</strong> {plan[1]} - {plan[2]} min - UGX {plan[3]:,}</p>
            <p><strong>Pay to:</strong></p>
            <p>MTN: 0785686404 | Airtel: 0751318876</p>
            <p style="color:#666;">Name: Rocky Peter Abayo</p>
            <hr>
            <p>After payment, paste the full SMS below:</p>
            <form method="POST">
                <input type="hidden" name="phone" value="{phone}">
                <input type="hidden" name="plan_id" value="{plan_id}">
                <label>Paste Full MTN/Airtel SMS Here</label>
                <textarea name="raw_sms" rows="6" required></textarea>
                <button type="submit" class="btn" style="margin-top:20px; width:100%;">Verify Payment</button>
            </form>
        </div>
    """
    return render_page("Verify Payment", content, pending_count)

# ------------------------------------------------------------
# ADMIN ROUTES
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
            return redirect('/dashboard')
        content = '<div class="card"><div class="alert alert-error">Invalid credentials.</div><p><a href="/login">Try again</a></p></div>'
        return render_page("Admin Login", content, 0)
    content = """
        <div class="card">
            <div class="card-header">Provider Login</div>
            <form method="POST">
                <label>Phone Number</label>
                <input type="tel" name="contact" required>
                <label>Password</label>
                <input type="password" name="password" required>
                <button type="submit" class="btn" style="margin-top:20px; width:100%;">Login</button>
            </form>
        </div>
    """
    return render_page("Admin Login", content, 0)

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
        <div class="card">
            <h2>Welcome, {session['provider_name']}</h2>
            <div style="display:flex; align-items:center; gap:15px; margin-top:15px;">
                <p><strong>Auto-Approval:</strong> <span style="color:{auto_color}; font-weight:700;">{auto_status}</span></p>
                <a href="/toggle-auto" class="btn btn-small" style="background:{'#dc3545' if auto_approve else '#28a745'};">Turn {'OFF' if auto_approve else 'ON'}</a>
            </div>
        </div>
        <div class="stat-grid">
            <div class="card" style="text-align:center;"><h3>UGX {sms_rev or 0:,}</h3><small>SMS Revenue Today</small></div>
            <div class="card" style="text-align:center;"><h3>UGX {cash_rev or 0:,}</h3><small>Cash Revenue Today</small></div>
            <div class="card" style="text-align:center;"><h3>{pending}</h3><small>Pending Approvals</small></div>
        </div>
        <div class="platform-revenue">
            <strong>RockabyTech Platform Fee (5% this week):</strong> UGX {weekly_fee:,} &nbsp; <small>({week_start.strftime('%d %b')} - {week_end.strftime('%d %b')})</small>
        </div>
        <div class="card">
            <div class="card-header">Quick Actions</div>
            <a href="/pending" class="btn btn-outline" style="margin:5px;">Review Pending</a>
            <a href="/generate-cash" class="btn" style="margin:5px;">Generate Cash Voucher</a>
            <a href="/plans" class="btn btn-outline" style="margin:5px;">Manage Plans</a>
            <a href="/provider/edit" class="btn btn-outline" style="margin:5px;">Settings</a>
        </div>
    """
    return render_page("Dashboard", content, pending, provider_id)

@app.route('/toggle-auto')
@login_required
def toggle_auto():
    current = get_auto_approve()
    new_val = 0 if current else 1
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("UPDATE providers SET auto_approve=? WHERE id=?", (new_val, session['provider_id']))
    conn.commit()
    conn.close()
    return redirect('/dashboard')

# ---- PLAN MANAGEMENT ----
@app.route('/plans')
@login_required
def list_plans():
    provider_id = session['provider_id']
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT id, name, duration_minutes, price_ugx, is_active FROM plans WHERE provider_id=?", (provider_id,))
    plans = c.fetchall()
    conn.close()
    rows = ""
    for p in plans:
        pid, name, mins, price, active = p
        status = "Active" if active else "Inactive"
        rows += f"""
        <tr>
            <td>{name}</td>
            <td>{mins} min</td>
            <td>UGX {price:,}</td>
            <td>{status}</td>
            <td>
                <a href="/plans/edit/{pid}" class="btn btn-small">Edit</a>
                <a href="/plans/delete/{pid}" class="btn btn-small btn-danger" onclick="return confirm('Delete this plan?')">Del</a>
            </td>
        </tr>"""
    content = f"""
        <div class="card">
            <div class="card-header">My Plans</div>
            <a href="/plans/add" class="btn btn-success" style="margin-bottom:15px;">+ Add Plan</a>
            <table>
                <tr><th>Name</th><th>Duration</th><th>Price</th><th>Status</th><th>Action</th></tr>
                {rows or '<tr><td colspan="5">No plans yet.</td></tr>'}
            </table>
        </div>
    """
    return render_page("Manage Plans", content, get_pending_count())

@app.route('/plans/add', methods=['GET', 'POST'])
@login_required
def add_plan():
    if request.method == 'POST':
        name = request.form['name']
        mins = int(request.form['duration'])
        price = int(request.form['price'])
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx) VALUES (?,?,?,?)",
                  (session['provider_id'], name, mins, price))
        conn.commit()
        conn.close()
        return redirect('/plans')
    content = """
        <div class="card">
            <div class="card-header">Add Plan</div>
            <form method="POST">
                <label>Plan Name</label><input type="text" name="name" required>
                <label>Duration (minutes)</label><input type="number" name="duration" required>
                <label>Price (UGX)</label><input type="number" name="price" required>
                <button type="submit" class="btn" style="margin-top:20px;">Save</button>
            </form>
        </div>
    """
    return render_page("Add Plan", content, get_pending_count())

@app.route('/plans/edit/<int:plan_id>', methods=['GET', 'POST'])
@login_required
def edit_plan(plan_id):
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT name, duration_minutes, price_ugx, is_active FROM plans WHERE id=? AND provider_id=?", (plan_id, session['provider_id']))
    plan = c.fetchone()
    if not plan:
        conn.close()
        return "Plan not found.", 404
    if request.method == 'POST':
        name = request.form['name']
        mins = int(request.form['duration'])
        price = int(request.form['price'])
        active = int(request.form.get('is_active', '1'))
        c.execute("UPDATE plans SET name=?, duration_minutes=?, price_ugx=?, is_active=? WHERE id=?",
                  (name, mins, price, active, plan_id))
        conn.commit()
        conn.close()
        return redirect('/plans')
    content = f"""
        <div class="card">
            <div class="card-header">Edit Plan</div>
            <form method="POST">
                <label>Name</label><input type="text" name="name" value="{plan[0]}" required>
                <label>Duration (min)</label><input type="number" name="duration" value="{plan[1]}" required>
                <label>Price (UGX)</label><input type="number" name="price" value="{plan[2]}" required>
                <label>Active</label>
                <select name="is_active">
                    <option value="1" {'selected' if plan[3] else ''}>Yes</option>
                    <option value="0" {'selected' if not plan[3] else ''}>No</option>
                </select>
                <button type="submit" class="btn" style="margin-top:20px;">Update</button>
            </form>
        </div>
    """
    conn.close()
    return render_page("Edit Plan", content, get_pending_count())

@app.route('/plans/delete/<int:plan_id>')
@login_required
def delete_plan(plan_id):
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("DELETE FROM plans WHERE id=? AND provider_id=?", (plan_id, session['provider_id']))
    conn.commit()
    conn.close()
    return redirect('/plans')

# ---- PROVIDER SETTINGS (edit poster, support phone, etc.) ----
@app.route('/provider/edit', methods=['GET', 'POST'])
@login_required
def edit_provider():
    provider = get_provider(session['provider_id'])
    if request.method == 'POST':
        business_name = request.form['business_name']
        support_phone = request.form['support_phone']
        file = request.files.get('poster')
        filename = provider[11]  # keep old poster
        if file and allowed_file(file.filename):
            # Force-create the uploads folder with full path
            upload_path = os.path.join(os.getcwd(), 'static', 'uploads')
            os.makedirs(upload_path, exist_ok=True)
            filename = secure_filename(file.filename)
            file.save(os.path.join(upload_path, filename))
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("UPDATE providers SET business_name=?, support_phone=?, poster_image=? WHERE id=?",
                  (business_name, support_phone, filename, session['provider_id']))
        conn.commit()
        conn.close()
        session['provider_name'] = business_name
        return redirect('/dashboard')

    content = f"""
        <div class="card">
            <div class="card-header">Provider Settings</div>
            <form method="POST" enctype="multipart/form-data">
                <label>Business Name</label>
                <input type="text" name="business_name" value="{provider[1]}" required>
                <label>Support WhatsApp (e.g., 256751318876)</label>
                <input type="text" name="support_phone" value="{provider[12] or ''}">
                <label>Portal Poster/Logo</label>
                <input type="file" name="poster" accept="image/*">
                {'<p>Current: <img src="/static/uploads/' + provider[11] + '" style="max-width:200px; border-radius:8px;"></p>' if provider[11] else ''}
                <button type="submit" class="btn" style="margin-top:20px;">Save Settings</button>
            </form>
        </div>
    """
    return render_page("Settings", content, get_pending_count())

@app.route('/pending')
@login_required
def pending():
    provider_id = session['provider_id']
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("""SELECT vr.id, vr.phone_number, pl.name, vr.amount, vr.transaction_id, vr.created_at
                 FROM voucher_requests vr JOIN plans pl ON vr.plan_id = pl.id
                 WHERE vr.provider_id=? AND vr.status='pending' ORDER BY vr.created_at DESC""", (provider_id,))
    pending_list = c.fetchall()
    conn.close()

    rows = ""
    for req in pending_list:
        rid, phone, plan_name, amount, tid, created = req
        rows += f"""
        <tr>
            <td>{phone}</td>
            <td>{plan_name}</td>
            <td>UGX {amount or 0:,}</td>
            <td>{tid}</td>
            <td>{str(created)[:16] if created else ''}</td>
            <td>
                <a href="/approve/{rid}" class="btn btn-small btn-success">Approve</a>
                <a href="/reject/{rid}" class="btn btn-small btn-danger">Reject</a>
            </td>
        </tr>"""
    if not rows:
        rows = "<tr><td colspan='6'>No pending requests.</td></tr>"

    content = f"""
        <div class="card">
            <div class="card-header">Pending Approvals</div>
            <table>
                <tr><th>Phone</th><th>Plan</th><th>Amount</th><th>Transaction ID</th><th>Time</th><th>Action</th></tr>
                {rows}
            </table>
        </div>
    """
    return render_page("Pending Approvals", content, len(pending_list))

@app.route('/approve/<int:req_id>')
@login_required
def approve(req_id):
    provider_id = session['provider_id']
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT phone_number, plan_id FROM voucher_requests WHERE id=? AND provider_id=?", (req_id, provider_id))
    req = c.fetchone()
    if req:
        code = generate_voucher_code()
        c.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number) VALUES (?, ?, ?, 'sms', ?)",
                  (provider_id, code, req[1], req[0]))
        c.execute("UPDATE voucher_requests SET status='approved', voucher_code=? WHERE id=?", (code, req_id))
        conn.commit()
    conn.close()
    return redirect('/pending')

@app.route('/reject/<int:req_id>')
@login_required
def reject(req_id):
    provider_id = session['provider_id']
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("UPDATE voucher_requests SET status='rejected' WHERE id=? AND provider_id=?", (req_id, provider_id))
    conn.commit()
    conn.close()
    return redirect('/pending')

@app.route('/generate-cash', methods=['GET', 'POST'])
@login_required
def generate_cash():
    provider_id = session['provider_id']
    pending_count = get_pending_count()

    if request.method == 'POST':
        plan_id = int(request.form['plan_id'])
        phone = request.form.get('phone', '').strip()
        code = generate_voucher_code()
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("INSERT INTO vouchers (provider_id, code, plan_id, payment_method, phone_number) VALUES (?, ?, ?, 'cash', ?)",
                  (provider_id, code, plan_id, phone))
        conn.commit()
        conn.close()
        content = f"""
            <div class="card">
                <div class="alert alert-success">Cash voucher generated!</div>
                <p><strong>Voucher Code:</strong></p>
                <div class="voucher-code">{code}</div>
                <p>Give this code to the customer.</p>
                <a href="/generate-cash" class="btn">Generate Another</a>
                <a href="/dashboard" class="btn btn-outline">Dashboard</a>
            </div>
        """
        return render_page("Voucher Generated", content, pending_count)

    content = f"""
        <div class="card">
            <div class="card-header">Generate Cash Voucher</div>
            <form method="POST">
                <label>Select Plan</label>
                <select name="plan_id" required>
                    {get_plan_options(provider_id)}
                </select>
                <label>Customer Phone (optional)</label>
                <input type="tel" name="phone">
                <button type="submit" class="btn" style="margin-top:20px; width:100%;">Generate</button>
            </form>
        </div>
    """
    return render_page("Generate Cash Voucher", content, pending_count)

@app.route('/stats')
@login_required
def stats():
    provider_id = session['provider_id']
    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM voucher_requests WHERE provider_id=? AND status='approved' AND date(created_at)=?", (provider_id, today))
    sms_count, sms_rev = c.fetchone()
    c.execute("SELECT COUNT(*), COALESCE(SUM(plans.price_ugx),0) FROM vouchers v JOIN plans ON v.plan_id=plans.id WHERE v.provider_id=? AND v.payment_method='cash' AND date(v.created_at)=?", (provider_id, today))
    cash_count, cash_rev = c.fetchone()

    c.execute("SELECT COUNT(*) FROM vouchers WHERE provider_id=? AND used=1", (provider_id,))
    used_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM vouchers WHERE provider_id=? AND used=0", (provider_id,))
    unused_count = c.fetchone()[0]

    c.execute("SELECT p.name, COUNT(*) FROM vouchers v JOIN plans p ON v.plan_id=p.id WHERE v.provider_id=? GROUP BY p.name ORDER BY COUNT(*) DESC", (provider_id,))
    plan_stats = c.fetchall()

    pending_count = get_pending_count()
    weekly_fee, week_start, week_end = get_weekly_platform_revenue()

    # Revenue last 7 days
    c.execute("""SELECT date(created_at) as day, COALESCE(SUM(amount),0) FROM voucher_requests
                 WHERE provider_id=? AND status='approved' AND created_at >= date('now', '-7 days')
                 GROUP BY day ORDER BY day""", (provider_id,))
    sms_daily = dict(c.fetchall())
    c.execute("""SELECT date(v.created_at) as day, COALESCE(SUM(pl.price_ugx),0) FROM vouchers v JOIN plans pl ON v.plan_id=pl.id
                 WHERE v.provider_id=? AND v.payment_method='cash' AND v.created_at >= date('now', '-7 days')
                 GROUP BY day ORDER BY day""", (provider_id,))
    cash_daily = dict(c.fetchall())

    last_7 = [(date.today() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    max_rev = max([sms_daily.get(d,0) + cash_daily.get(d,0) for d in last_7] + [1])
    bar_html = ""
    for d in last_7:
        sms = sms_daily.get(d, 0)
        cash = cash_daily.get(d, 0)
        total = sms + cash
        pct = int((total / max_rev) * 100) if max_rev > 0 else 0
        bar_html += f"""
        <div style="display:flex; align-items:center; margin:4px 0; font-size:0.8rem;">
            <div style="width:60px;">{d[5:]}</div>
            <div style="flex:1; background:#eee; height:20px; border-radius:4px;">
                <div style="width:{pct}%; background:var(--primary); height:100%; border-radius:4px;"></div>
            </div>
            <div style="width:80px; text-align:right;">UGX {total:,}</div>
        </div>"""

    conn.close()

    content = f"""
        <div class="stat-grid">
            <div class="card" style="text-align:center;"><h3>UGX {sms_rev or 0:,}</h3><small>SMS Revenue Today</small></div>
            <div class="card" style="text-align:center;"><h3>UGX {cash_rev or 0:,}</h3><small>Cash Revenue Today</small></div>
            <div class="card" style="text-align:center;"><h3>{used_count}</h3><small>Vouchers Used</small></div>
            <div class="card" style="text-align:center;"><h3>{unused_count}</h3><small>Vouchers Unused</small></div>
            <div class="card" style="text-align:center;"><h3>{pending_count}</h3><small>Pending</small></div>
        </div>
        <div class="platform-revenue">
            <strong>RockabyTech Platform Fee (5% this week):</strong> UGX {weekly_fee:,} &nbsp; <small>({week_start.strftime('%d %b')} - {week_end.strftime('%d %b')})</small>
        </div>
        <div class="card">
            <div class="card-header">Revenue Last 7 Days (SMS + Cash)</div>
            {bar_html}
        </div>
        <div class="card">
            <div class="card-header">Top Selling Plans</div>
            <table>
                <tr><th>Plan</th><th>Sold</th></tr>
                {''.join(f'<tr><td>{p[0]}</td><td>{p[1]}</td></tr>' for p in plan_stats) or '<tr><td colspan="2">No sales yet.</td></tr>'}
            </table>
        </div>
        <a href="/dashboard" class="btn btn-outline">Back to Dashboard</a>
    """
    return render_page("Statistics", content, pending_count)

# ------------------------------------------------------------
init_db()
if __name__ == '__main__':
    app.run()
