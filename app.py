import os, sqlite3, re, random, string
from datetime import date, timedelta, datetime
from collections import defaultdict
from flask import Flask, render_template_string, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
app.secret_key = 'rockabywifi-secret-key-change-in-production'

# ------------------------------------------------------------
# DATABASE SETUP
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
        airtel_number TEXT
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

     # Settings table (MUST be created before inserting)
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        FOREIGN KEY(provider_id) REFERENCES providers(id)
    )''')

    # Default super admin
    c.execute("SELECT COUNT(*) FROM providers WHERE id=1")
    if c.fetchone()[0] == 0:
        hashed = generate_password_hash('admin123')
        c.execute("INSERT INTO providers (business_name, contact, password_hash, subscription_expiry, is_active) VALUES (?,?,?,?,?)",
                  ('RockabyWiFi Admin', '256787654321', hashed, date.today() + timedelta(days=3650), 1))
        # Default plans
        for name, mins, price in [('1 Hour', 60, 1000), ('3 Hours', 180, 2500), ('1 Day', 1440, 5000), ('1 Week', 10080, 20000)]:
            c.execute("INSERT INTO plans (provider_id, name, duration_minutes, price_ugx) VALUES (1, ?, ?, ?)", (name, mins, price))
        # Default settings
        c.execute("INSERT INTO settings (provider_id, key, value) VALUES (1, 'auto_approve', '1')")

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
    recipient = re.search(r'to\s+(.+?),', sms)
    date_str = re.search(r'on\s+(\d{4}-\d{2}-\d{2})', sms)
    return {
        'tid': tid.group(1) if tid else None,
        'amount': int(amount.group(1).replace(',','')) if amount else None,
        'recipient': recipient.group(1).strip() if recipient else None,
        'date': date_str.group(1) if date_str else None
    }

def parse_airtel_sms(sms):
    tid = re.search(r'TID\s*(\d+)', sms)
    amount = re.search(r'UGX\s*([\d,]+)', sms)
    recipient = re.search(r'to\s+(.+?)\s+\d', sms)
    date_str = re.search(r'Date\s+(\d{2}-[A-Za-z]+-\d{4}\s+\d{2}:\d{2})', sms)
    return {
        'tid': tid.group(1) if tid else None,
        'amount': int(amount.group(1).replace(',','')) if amount else None,
        'recipient': recipient.group(1).strip() if recipient else None,
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
        opts += f'<option value="{p[0]}">{p[1]} – {p[2]} min – UGX {p[3]:,}</option>'
    return opts

# ------------------------------------------------------------
# BASE TEMPLATE
# ------------------------------------------------------------
base_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RockabyWiFi – {title}</title>
    {% raw %}
    <style>
        :root {
            --primary: #1a73e8; --primary-dark: #1557b0;
            --bg: #f0f4f8; --card-bg: #ffffff; --text: #1a1a1a;
            --text-secondary: #666666; --border: #e0e0e0;
            --radius: 12px; --shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg); color: var(--text); min-height: 100vh;
        }
        .navbar {
            background: var(--card-bg); box-shadow: 0 1px 3px rgba(0,0,0,0.08);
            padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;
        }
        .navbar .logo { font-size: 1.3rem; font-weight: 700; color: var(--primary); text-decoration: none; }
        .nav-links { display: flex; gap: 15px; flex-wrap: wrap; }
        .nav-links a { color: var(--text-secondary); text-decoration: none; font-weight: 500; }
        .nav-links a:hover { color: var(--primary); }
        .btn {
            display: inline-block; padding: 10px 20px; background: var(--primary);
            color: #fff; border: none; border-radius: 6px; font-weight: 600;
            cursor: pointer; text-decoration: none;
        }
        .btn:hover { background: var(--primary-dark); }
        .btn-outline { background: transparent; border: 1px solid var(--primary); color: var(--primary); }
        .btn-small { padding: 5px 10px; font-size: 0.8rem; }
        .btn-danger { background: #dc3545; }
        .container { max-width: 700px; margin: 20px auto; padding: 0 15px; }
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
        .alert { padding: 10px 15px; border-radius: 6px; margin-bottom: 15px; }
        .alert-success { background: #d4edda; color: #155724; }
        .alert-error { background: #f8d7da; color: #721c24; }
        footer { text-align: center; color: var(--text-secondary); padding: 30px 0; font-size: 0.9rem; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 8px; text-align: left; border-bottom: 1px solid var(--border); }
        @media (max-width: 600px) {
            .navbar { flex-direction: column; gap: 10px; }
        }
    </style>
    {% endraw %}
</head>
<body>
    <nav class="navbar">
        <a href="/" class="logo">📡 ROCKABYWIFI</a>
        <div class="nav-links">
            {% if session.provider_id %}
                <a href="/dashboard">Dashboard</a>
                <a href="/logout">Logout</a>
            {% else %}
                <a href="/login">Admin Login</a>
            {% endif %}
        </div>
    </nav>
    <div class="container">
        {content}
    </div>
    <footer>
        &copy; 2025 RockabyTech – WiFi Billing Made Simple
    </footer>
</body>
</html>
"""

# ------------------------------------------------------------
# CUSTOMER ROUTES
# ------------------------------------------------------------
@app.route('/')
def home():
    return render_template_string(base_template.replace("{title}", "Get Internet Access").replace("{content}", f"""
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
    """))

@app.route('/sms-verify', methods=['GET', 'POST'])
def sms_verify():
    phone = request.args.get('phone', '')
    plan_id = request.args.get('plan_id', '1')

    conn = sqlite3.connect('rockabywifi.db')
    c = conn.cursor()
    c.execute("SELECT id, name, duration_minutes, price_ugx FROM plans WHERE id=?", (plan_id,))
    plan = c.fetchone()
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
            error = "Could not detect Transaction ID. Please paste the full SMS."
        elif not parsed['amount']:
            error = "Could not detect amount. Please paste the full SMS."

        if error:
            return render_template_string(base_template.replace("{title}", "Verify Payment").replace("{content}", f"""
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
            """))

        # Save request
        conn = sqlite3.connect('rockabywifi.db')
        c = conn.cursor()
        c.execute("""INSERT INTO voucher_requests (provider_id, phone_number, plan_id, raw_sms, transaction_id, amount, recipient, payment_date, status)
                     VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                  (phone, plan_id, raw_sms, parsed['tid'], parsed['amount'], parsed['recipient'], parsed['date']))
        conn.commit()
        conn.close()

        return render_template_string(base_template.replace("{title}", "Verification Submitted").replace("{content}", """
            <div class="card">
                <div class="alert alert-success">Payment submitted! If auto-approval is on and your SMS is valid, your voucher will be ready shortly. Otherwise, wait for manual approval.</div>
                <p><a href="/" class="btn">Back to Home</a></p>
            </div>
        """))

    return render_template_string(base_template.replace("{title}", "Verify Payment").replace("{content}", f"""
        <div class="card">
            <div class="card-header">Pay for Internet</div>
            <p><strong>Selected Plan:</strong> {plan[1]} – {plan[2]} min – UGX {plan[3]:,}</p>
            <p><strong>Pay to:</strong></p>
            <p>MTN: 0785686404 | Airtel: 0751318876</p>
            <p style="color:#666;">Name: Rocky Peter Abayo</p>
            <hr>
            <p>After payment, paste the full SMS you receive from MTN/Airtel below:</p>
            <form method="POST">
                <input type="hidden" name="phone" value="{phone}">
                <input type="hidden" name="plan_id" value="{plan_id}">
                <label>Paste Full MTN/Airtel SMS Here</label>
                <textarea name="raw_sms" rows="6" required></textarea>
                <button type="submit" class="btn" style="margin-top:20px; width:100%;">Verify Payment</button>
            </form>
        </div>
    """))

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
        return "Invalid credentials. <a href='/login'>Try again</a>"
    return render_template_string(base_template.replace("{title}", "Admin Login").replace("{content}", """
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
    """))

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
    c.execute("SELECT COUNT(*) FROM voucher_requests WHERE provider_id=? AND status='pending'", (provider_id,))
    pending = c.fetchone()[0]
    conn.close()

    return render_template_string(base_template.replace("{title}", "Dashboard").replace("{content}", f"""
        <div class="card"><h2>Welcome, {session['provider_name']}</h2></div>
        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap:10px;">
            <div class="card" style="text-align:center;"><h3>UGX {sms_rev:,}</h3><small>SMS Revenue Today</small></div>
            <div class="card" style="text-align:center;"><h3>UGX {cash_rev:,}</h3><small>Cash Revenue Today</small></div>
            <div class="card" style="text-align:center;"><h3>{pending}</h3><small>Pending Approvals</small></div>
        </div>
        <div class="card">
            <div class="card-header">Quick Actions</div>
            <a href="/pending" class="btn btn-outline" style="margin:5px;">Review Pending</a>
            <a href="/generate-cash" class="btn" style="margin:5px;">Generate Cash Voucher</a>
            <a href="/stats" class="btn btn-outline" style="margin:5px;">Full Statistics</a>
        </div>
    """))

# Placeholder routes for future completion
@app.route('/pending')
@login_required
def pending():
    return "Pending approvals page – coming next."

@app.route('/generate-cash', methods=['GET', 'POST'])
@login_required
def generate_cash():
    if request.method == 'POST':
        return "Cash voucher generated – full logic coming next."
    return render_template_string(base_template.replace("{title}", "Generate Cash Voucher").replace("{content}", """
        <div class="card">
            <div class="card-header">Generate Cash Voucher</div>
            <form method="POST">
                <label>Select Plan</label>
                <select name="plan_id" required>
                    """ + get_plan_options(session['provider_id']) + """
                </select>
                <label>Customer Phone Number (optional)</label>
                <input type="tel" name="phone">
                <button type="submit" class="btn" style="margin-top:20px; width:100%;">Generate Voucher</button>
            </form>
        </div>
    """))

@app.route('/stats')
@login_required
def stats():
    return "Full statistics page – coming next."

init_db()

if __name__ == '__main__':
    app.run()
