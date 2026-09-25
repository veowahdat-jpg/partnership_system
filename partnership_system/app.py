# -*- coding: utf-8 -*-
"""
سیستم مدیریت شراکت - فروش و سرویس
نسخه حرفه‌ای با یوزرنیم و پسورد جداگانه
"""

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import sqlite3
import os
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "partnership-secret-key-change-in-production-2026")
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024  # 2MB
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# مسیر دیتابیس محلی
DB_PATH = os.path.join(os.path.dirname(__file__), "partnership.db")

# تنظیمات Turso (اگر وجود داشته باشد از Turso استفاده می‌شود)
TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "لطفاً ابتدا وارد شوید."
login_manager.login_message_category = "warning"

# ==================== Database Helpers ====================

class DictRow(dict):
    """شبیه sqlite3.Row برای دسترسی هم با نام و هم با ایندکس"""
    def __init__(self, columns, values):
        super().__init__(zip(columns, values))
        self._values = list(values)
        self._columns = list(columns)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


class TursoConnection:
    """لایه سازگار با sqlite3 برای Turso (libsql-client)"""
    def __init__(self, client):
        self.client = client
        self._last_cursor = None

    def execute(self, sql, params=()):
        result = self.client.execute(sql, list(params) if params else None)
        self._last_cursor = TursoCursor(result)
        return self._last_cursor

    def fetchone(self):
        return self._last_cursor.fetchone() if self._last_cursor else None

    def fetchall(self):
        return self._last_cursor.fetchall() if self._last_cursor else []

    def executemany(self, sql, seq_of_params):
        for params in seq_of_params:
            self.execute(sql, params)
        return self

    def commit(self):
        pass

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    def cursor(self):
        return self


class TursoCursor:
    def __init__(self, result):
        self.columns = list(result.columns) if getattr(result, "columns", None) else []
        raw = result.rows if getattr(result, "rows", None) is not None else []
        self._rows = [DictRow(self.columns, row) for row in raw]
        self._index = 0

    def fetchone(self):
        if self._index < len(self._rows):
            row = self._rows[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self._rows)

def get_db():
    if USE_TURSO:
        try:
            from libsql_client import create_client_sync
            client = create_client_sync(url=TURSO_URL, auth_token=TURSO_TOKEN)
            return TursoConnection(client)
        except Exception as e:
            print(f"[Turso] Connection error: {e} — falling back to local SQLite")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    # کاربران
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'partner')),
            partner_code TEXT,
            photo TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # مواد / محصولات
    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            unit TEXT DEFAULT 'عدد',
            buy_price REAL DEFAULT 0,
            sell_price REAL DEFAULT 0,
            initial_stock REAL DEFAULT 0,
            description TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # فروش‌ها
    c.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_date TEXT NOT NULL,
            month TEXT NOT NULL,
            seller_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            unit_sell_price REAL NOT NULL,
            unit_buy_price REAL NOT NULL,
            total_sale REAL NOT NULL,
            total_cost REAL NOT NULL,
            profit REAL NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (seller_id) REFERENCES users(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    # مشتریان
    c.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            phone TEXT,
            address TEXT,
            service_type TEXT,
            amount REAL DEFAULT 0,
            notes TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # پرداخت‌های سرویس
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_date TEXT NOT NULL,
            month TEXT NOT NULL,
            customer_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            received_by INTEGER NOT NULL,
            payment_type TEXT DEFAULT 'نقد',
            status TEXT DEFAULT 'پرداخت کامل',
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (received_by) REFERENCES users(id)
        )
    """)

    # مصارف و برداشت‌ها (بدون CHECK سخت‌گیرانه تا با دیتابیس قدیمی سازگار باشد)
    c.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expense_date TEXT NOT NULL,
            month TEXT NOT NULL,
            partner_id INTEGER,
            expense_type TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            approved INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (partner_id) REFERENCES users(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)

    # لاگ فعالیت‌ها
    c.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            ip_address TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # تنظیمات شرکت
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # مهاجرت برای اضافه کردن ستون photo اگر وجود نداشته باشد
    try:
        c.execute("ALTER TABLE users ADD COLUMN photo TEXT")
    except:
        pass

    try:
        c.execute("ALTER TABLE customers ADD COLUMN amount REAL DEFAULT 0")
    except:
        pass

    # جدول اضافه‌کاری
    c.execute("""
        CREATE TABLE IF NOT EXISTS overtime (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            overtime_date TEXT NOT NULL,
            month TEXT NOT NULL,
            partner_id INTEGER,
            amount REAL NOT NULL,
            description TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (partner_id) REFERENCES users(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)

    # واریز دستی به دخل (فقط ادمین)
    c.execute("""
        CREATE TABLE IF NOT EXISTS cash_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deposit_date TEXT NOT NULL,
            month TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)

    # ثبت ماهانه (بستن حساب ماه)
    c.execute("""
        CREATE TABLE IF NOT EXISTS monthly_closings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month TEXT UNIQUE NOT NULL,
            closed_at TEXT NOT NULL,
            closed_by INTEGER,
            total_sales REAL DEFAULT 0,
            total_sales_profit REAL DEFAULT 0,
            total_service REAL DEFAULT 0,
            total_overtime REAL DEFAULT 0,
            total_common_exp REAL DEFAULT 0,
            total_personal_exp REAL DEFAULT 0,
            total_withdrawals REAL DEFAULT 0,
            total_cash_in REAL DEFAULT 0,
            total_cash_out REAL DEFAULT 0,
            cash_balance REAL DEFAULT 0,
            notes TEXT,
            FOREIGN KEY (closed_by) REFERENCES users(id)
        )
    """)


    # --- مهاجرت امن برای دیتابیس‌های قبلی (Turso/موجود) ---
    # اگر جدول expenses با CHECK قدیمی باشد، آن را بدون از دست دادن داده ارتقا می‌دهیم
    try:
        # تست: آیا می‌توان نوع جدید را insert کرد؟ اگر نه، جدول را ارتقا بده
        test = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='expenses'").fetchone()
        if test:
            sql_def = str(test[0] if not hasattr(test, 'keys') else test['sql'] if 'sql' in test else test[0])
            if 'برداشت از دخل' not in sql_def and 'CHECK' in sql_def:
                print("[Migration] Upgrading expenses table for new expense types...")
                c.execute("""
                    CREATE TABLE IF NOT EXISTS expenses_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        expense_date TEXT NOT NULL,
                        month TEXT NOT NULL,
                        partner_id INTEGER,
                        expense_type TEXT NOT NULL,
                        amount REAL NOT NULL,
                        description TEXT,
                        approved INTEGER DEFAULT 1,
                        created_by INTEGER,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                c.execute("""
                    INSERT INTO expenses_new (id, expense_date, month, partner_id, expense_type, amount, description, approved, created_by, created_at)
                    SELECT id, expense_date, month, partner_id, expense_type, amount, description, approved, created_by, created_at FROM expenses
                """)
                c.execute("DROP TABLE expenses")
                c.execute("ALTER TABLE expenses_new RENAME TO expenses")
                print("[Migration] expenses table upgraded successfully - data preserved")
    except Exception as e:
        print(f"[Migration] expenses upgrade skipped or failed (safe): {e}")

    # اطمینان از وجود جدول‌های جدید
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS cash_deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deposit_date TEXT NOT NULL,
                month TEXT NOT NULL,
                amount REAL NOT NULL,
                description TEXT,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    except Exception as e:
        print(f"[Migration] cash_deposits: {e}")

    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS monthly_closings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT UNIQUE NOT NULL,
                closed_at TEXT NOT NULL,
                closed_by INTEGER,
                total_sales REAL DEFAULT 0,
                total_sales_profit REAL DEFAULT 0,
                total_service REAL DEFAULT 0,
                total_overtime REAL DEFAULT 0,
                total_common_exp REAL DEFAULT 0,
                total_personal_exp REAL DEFAULT 0,
                total_withdrawals REAL DEFAULT 0,
                total_cash_in REAL DEFAULT 0,
                total_cash_out REAL DEFAULT 0,
                cash_balance REAL DEFAULT 0,
                notes TEXT
            )
        """)
    except Exception as e:
        print(f"[Migration] monthly_closings: {e}")


    conn.commit()

    # تنظیمات پیش‌فرض شرکت (همیشه)
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('company_name', 'شرکت اعتماد')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('logo', '')")
    conn.commit()

    # کاربران پیش‌فرض
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        users = [
            ("admin", generate_password_hash("admin123"), "ادمین سیستم", "admin", None),
            ("partner1", generate_password_hash("p1123"), "شریک ۱", "partner", "P1"),
            ("partner2", generate_password_hash("p2123"), "شریک ۲", "partner", "P2"),
            ("partner3", generate_password_hash("p3123"), "شریک ۳", "partner", "P3"),
        ]
        c.executemany(
            "INSERT INTO users (username, password_hash, full_name, role, partner_code) VALUES (?, ?, ?, ?, ?)",
            users
        )

        # محصولات نمونه
        products = [
            ("M001", "سیمان پاکتی", "پاکت", 350, 450, 100, "سیمان تیپ ۲"),
            ("M002", "آهن ۱۲ میل", "شاخه", 1200, 1500, 50, ""),
            ("M003", "گچ ساختمانی", "کیسه", 180, 250, 80, ""),
            ("M004", "آجر فشاری", "عدد", 8, 12, 5000, ""),
            ("M005", "شن و ماسه", "متر مکعب", 800, 1100, 30, ""),
        ]
        c.executemany(
            "INSERT INTO products (code, name, unit, buy_price, sell_price, initial_stock, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
            products
        )

        # مشتریان نمونه
        customers = [
            ("C001", "احمد رضایی", "0700123456", "کابل - کارته ۴", "تعمیرات ساختمانی", ""),
            ("C002", "محمد کریمی", "0788123456", "کابل - شهر نو", "نقاشی و گچ‌کاری", ""),
            ("C003", "فاطمه احمدی", "0799123456", "کابل - خیرخانه", "لوله کشی", ""),
            ("C004", "شرکت ساختمانی آریا", "0202123456", "کابل - پل خشتی", "پروژه بزرگ", "قرارداد ماهانه"),
        ]
        c.executemany(
            "INSERT INTO customers (code, name, phone, address, service_type, notes) VALUES (?, ?, ?, ?, ?, ?)",
            customers
        )
        conn.commit()

    conn.close()

# ==================== User Model ====================

class User(UserMixin):
    def __init__(self, id, username, full_name, role, partner_code, active=True):
        self.id = id
        self.username = username
        self.full_name = full_name
        self.role = role
        self.partner_code = partner_code
        self._active = bool(active)

    @property
    def is_active(self):
        return self._active

    @property
    def is_admin(self):
        return self.role == "admin"

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row and row["is_active"]:
        return User(row["id"], row["username"], row["full_name"], row["role"], row["partner_code"], row["is_active"])
    return None

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("دسترسی فقط برای ادمین مجاز است.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

def log_activity(action, details=""):
    if current_user.is_authenticated:
        conn = get_db()
        conn.execute(
            "INSERT INTO activity_log (user_id, action, details, ip_address) VALUES (?, ?, ?, ?)",
            (current_user.id, action, details, request.remote_addr)
        )
        conn.commit()
        conn.close()

# ==================== Routes ====================

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)).fetchone()
        conn.close()

        if row and check_password_hash(row["password_hash"], password):
            user = User(row["id"], row["username"], row["full_name"], row["role"], row["partner_code"], row["is_active"])
            login_user(user, remember=True)
            log_activity("ورود به سیستم")
            flash(f"خوش آمدید {user.full_name}!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("نام کاربری یا رمز عبور اشتباه است.", "danger")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    log_activity("خروج از سیستم")
    logout_user()
    flash("با موفقیت خارج شدید.", "info")
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    conn = get_db()

    # آمار فروش
    sales_stats = conn.execute("""
        SELECT COUNT(*) as count, COALESCE(SUM(total_sale),0) as total_sale, COALESCE(SUM(profit),0) as total_profit
        FROM sales WHERE month = ?
    """, (month,)).fetchone()

    # آمار سرویس
    service_stats = conn.execute("""
        SELECT COUNT(*) as count, COALESCE(SUM(amount),0) as total_amount
        FROM payments WHERE month = ?
    """, (month,)).fetchone()

    # مصارف مشترک
    common_exp = conn.execute("""
        SELECT COALESCE(SUM(amount),0) as total FROM expenses
        WHERE month = ? AND expense_type = 'مصرف مشترک'
    """, (month,)).fetchone()["total"]

    # موجودی کم
    low_stock = conn.execute("""
        SELECT p.name, p.initial_stock - COALESCE(SUM(s.quantity),0) as current_stock
        FROM products p
        LEFT JOIN sales s ON p.id = s.product_id
        WHERE p.is_active = 1
        GROUP BY p.id
        HAVING current_stock < 10
        ORDER BY current_stock
        LIMIT 5
    """).fetchall()

    # آخرین فروش‌ها
    recent_sales = conn.execute("""
        SELECT s.*, u.full_name as seller_name, p.name as product_name
        FROM sales s
        JOIN users u ON s.seller_id = u.id
        JOIN products p ON s.product_id = p.id
        ORDER BY s.id DESC LIMIT 8
    """).fetchall()

    # آخرین پرداخت‌ها
    recent_payments = conn.execute("""
        SELECT pay.*, c.name as customer_name, u.full_name as receiver_name
        FROM payments pay
        JOIN customers c ON pay.customer_id = c.id
        JOIN users u ON pay.received_by = u.id
        ORDER BY pay.id DESC LIMIT 8
    """).fetchall()

    conn.close()

    net_service = service_stats["total_amount"] - common_exp
    base_share = net_service / 3 if net_service else 0

    return render_template("dashboard.html",
                           month=month,
                           sales_stats=sales_stats,
                           service_stats=service_stats,
                           common_exp=common_exp,
                           net_service=net_service,
                           base_share=base_share,
                           low_stock=low_stock,
                           recent_sales=recent_sales,
                           recent_payments=recent_payments)

# ---------- محصولات ----------
@app.route("/products")
@login_required
def products():
    conn = get_db()
    products = conn.execute("""
        SELECT p.*, 
               p.initial_stock - COALESCE((SELECT SUM(quantity) FROM sales WHERE product_id = p.id), 0) as current_stock
        FROM products p
        WHERE p.is_active = 1
        ORDER BY p.code
    """).fetchall()
    conn.close()
    return render_template("products.html", products=products)

@app.route("/products/add", methods=["GET", "POST"])
@login_required
@admin_required
def product_add():
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "عدد").strip()
        buy_price = float(request.form.get("buy_price") or 0)
        sell_price = float(request.form.get("sell_price") or 0)
        initial_stock = float(request.form.get("initial_stock") or 0)
        description = request.form.get("description", "").strip()

        if not code or not name:
            flash("کد و نام ماده الزامی است.", "danger")
            return redirect(url_for("product_add"))

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO products (code, name, unit, buy_price, sell_price, initial_stock, description) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, name, unit, buy_price, sell_price, initial_stock, description)
            )
            conn.commit()
            log_activity("افزودن ماده", f"{code} - {name}")
            flash("ماده با موفقیت اضافه شد.", "success")
        except sqlite3.IntegrityError:
            flash("کد ماده تکراری است.", "danger")
        finally:
            conn.close()
        return redirect(url_for("products"))

    return render_template("product_form.html", product=None)

@app.route("/products/edit/<int:id>", methods=["GET", "POST"])
@login_required
@admin_required
def product_edit(id):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (id,)).fetchone()
    if not product:
        flash("ماده یافت نشد.", "danger")
        return redirect(url_for("products"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "عدد").strip()
        buy_price = float(request.form.get("buy_price") or 0)
        sell_price = float(request.form.get("sell_price") or 0)
        initial_stock = float(request.form.get("initial_stock") or 0)
        description = request.form.get("description", "").strip()

        conn.execute(
            "UPDATE products SET name=?, unit=?, buy_price=?, sell_price=?, initial_stock=?, description=? WHERE id=?",
            (name, unit, buy_price, sell_price, initial_stock, description, id)
        )
        conn.commit()
        log_activity("ویرایش ماده", f"ID {id}")
        flash("ماده به‌روزرسانی شد.", "success")
        conn.close()
        return redirect(url_for("products"))

    conn.close()
    return render_template("product_form.html", product=product)

# ---------- فروش ----------
@app.route("/sales")
@login_required
def sales():
    month = request.args.get("month", "")
    conn = get_db()
    query = """
        SELECT s.*, u.full_name as seller_name, p.name as product_name, p.code as product_code
        FROM sales s
        JOIN users u ON s.seller_id = u.id
        JOIN products p ON s.product_id = p.id
    """
    params = []
    if month:
        query += " WHERE s.month = ?"
        params.append(month)
    query += " ORDER BY s.sale_date DESC, s.id DESC"

    sales_list = conn.execute(query, params).fetchall()
    products = conn.execute("SELECT id, code, name, sell_price, buy_price FROM products WHERE is_active=1 ORDER BY code").fetchall()
    partners = conn.execute("SELECT id, full_name FROM users WHERE role='partner' AND is_active=1").fetchall()
    conn.close()
    return render_template("sales.html", sales=sales_list, products=products, partners=partners, month=month)

@app.route("/sales/add", methods=["POST"])
@login_required
def sale_add():
    sale_date = request.form.get("sale_date")
    product_id = int(request.form.get("product_id"))
    quantity = float(request.form.get("quantity") or 0)
    unit_sell_price = float(request.form.get("unit_sell_price") or 0)
    notes = request.form.get("notes", "").strip()

    # فروشنده: اگر ادمین باشد می‌تواند انتخاب کند، وگرنه خودش
    if current_user.is_admin:
        seller_id = int(request.form.get("seller_id") or current_user.id)
    else:
        seller_id = current_user.id

    if quantity <= 0:
        flash("تعداد باید بیشتر از صفر باشد.", "danger")
        return redirect(url_for("sales"))

    month = sale_date[:7] if sale_date else datetime.now().strftime("%Y-%m")

    conn = get_db()
    product = conn.execute("SELECT buy_price FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        flash("محصول یافت نشد.", "danger")
        conn.close()
        return redirect(url_for("sales"))

    unit_buy_price = product["buy_price"]
    total_sale = quantity * unit_sell_price
    total_cost = quantity * unit_buy_price
    profit = total_sale - total_cost

    conn.execute("""
        INSERT INTO sales (sale_date, month, seller_id, product_id, quantity, unit_sell_price, unit_buy_price,
                           total_sale, total_cost, profit, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (sale_date, month, seller_id, product_id, quantity, unit_sell_price, unit_buy_price,
          total_sale, total_cost, profit, notes))
    conn.commit()
    log_activity("ثبت فروش", f"محصول {product_id} - تعداد {quantity}")
    conn.close()
    flash("فروش با موفقیت ثبت شد.", "success")
    return redirect(url_for("sales"))

# ---------- مشتریان ----------
@app.route("/customers")
@login_required
def customers():
    conn = get_db()
    customers = conn.execute("SELECT * FROM customers WHERE is_active=1 ORDER BY code").fetchall()
    conn.close()
    return render_template("customers.html", customers=customers)

@app.route("/customers/add", methods=["GET", "POST"])
@login_required
@admin_required
def customer_add():
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        service_type = request.form.get("service_type", "").strip()
        amount = float(request.form.get("amount") or 0)
        notes = request.form.get("notes", "").strip()

        if not code or not name:
            flash("کد و نام مشتری الزامی است.", "danger")
            return redirect(url_for("customer_add"))

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO customers (code, name, phone, address, service_type, amount, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, name, phone, address, service_type, amount, notes)
            )
            conn.commit()
            log_activity("افزودن مشتری", f"{code} - {name}")
            flash("مشتری اضافه شد.", "success")
        except sqlite3.IntegrityError:
            flash("کد مشتری تکراری است.", "danger")
        finally:
            conn.close()
        return redirect(url_for("customers"))

    return render_template("customer_form.html", customer=None)

@app.route("/customers/edit/<int:id>", methods=["GET", "POST"])
@login_required
@admin_required
def customer_edit(id):
    conn = get_db()
    customer = conn.execute("SELECT * FROM customers WHERE id = ?", (id,)).fetchone()
    if not customer:
        flash("مشتری یافت نشد.", "danger")
        conn.close()
        return redirect(url_for("customers"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        service_type = request.form.get("service_type", "").strip()
        amount = float(request.form.get("amount") or 0)
        notes = request.form.get("notes", "").strip()

        conn.execute(
            "UPDATE customers SET name=?, phone=?, address=?, service_type=?, amount=?, notes=? WHERE id=?",
            (name, phone, address, service_type, amount, notes, id)
        )
        conn.commit()
        log_activity("ویرایش مشتری", name)
        flash("مشتری به‌روزرسانی شد.", "success")
        conn.close()
        return redirect(url_for("customers"))

    conn.close()
    return render_template("customer_form.html", customer=customer)

# ---------- پرداخت‌ها ----------
@app.route("/payments")
@login_required
def payments():
    month = request.args.get("month", "")
    conn = get_db()
    query = """
        SELECT pay.*, c.name as customer_name, c.code as customer_code, u.full_name as receiver_name
        FROM payments pay
        JOIN customers c ON pay.customer_id = c.id
        JOIN users u ON pay.received_by = u.id
    """
    params = []
    if month:
        query += " WHERE pay.month = ?"
        params.append(month)
    query += " ORDER BY pay.payment_date DESC, pay.id DESC"

    payments_list = conn.execute(query, params).fetchall()
    customers = conn.execute("SELECT id, code, name FROM customers WHERE is_active=1 ORDER BY code").fetchall()
    partners = conn.execute("SELECT id, full_name FROM users WHERE role='partner' AND is_active=1").fetchall()
    conn.close()
    return render_template("payments.html", payments=payments_list, customers=customers, partners=partners, month=month)

@app.route("/payments/add", methods=["POST"])
@login_required
def payment_add():
    payment_date = request.form.get("payment_date")
    customer_id = int(request.form.get("customer_id"))
    amount = float(request.form.get("amount") or 0)
    payment_type = request.form.get("payment_type", "نقد")
    status = request.form.get("status", "پرداخت کامل")
    notes = request.form.get("notes", "").strip()

    if current_user.is_admin:
        received_by = int(request.form.get("received_by") or current_user.id)
    else:
        received_by = current_user.id

    if amount <= 0:
        flash("مبلغ باید بیشتر از صفر باشد.", "danger")
        return redirect(url_for("payments"))

    month = payment_date[:7] if payment_date else datetime.now().strftime("%Y-%m")

    conn = get_db()
    conn.execute("""
        INSERT INTO payments (payment_date, month, customer_id, amount, received_by, payment_type, status, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (payment_date, month, customer_id, amount, received_by, payment_type, status, notes))
    conn.commit()
    log_activity("ثبت پرداخت سرویس", f"مبلغ {amount}")
    conn.close()
    flash("پرداخت با موفقیت ثبت شد.", "success")
    return redirect(url_for("payments"))

# ---------- مصارف و برداشت ----------
@app.route("/expenses")
@login_required
def expenses():
    month = request.args.get("month", "")
    conn = get_db()
    query = """
        SELECT e.*, u.full_name as partner_name, creator.full_name as created_by_name
        FROM expenses e
        LEFT JOIN users u ON e.partner_id = u.id
        LEFT JOIN users creator ON e.created_by = creator.id
    """
    params = []
    if month:
        query += " WHERE e.month = ?"
        params.append(month)
    query += " ORDER BY e.expense_date DESC, e.id DESC"

    expenses_list = conn.execute(query, params).fetchall()
    partners = conn.execute("SELECT id, full_name FROM users WHERE role='partner' AND is_active=1").fetchall()
    conn.close()

    # محاسبه قابلیت ویرایش (تا ۱ ساعت)
    now = datetime.now()
    editable_map = {}
    for e in expenses_list:
        try:
            created = datetime.strptime(str(e["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
            editable_map[e["id"]] = (now - created).total_seconds() <= 3600
        except Exception:
            editable_map[e["id"]] = False

    return render_template("expenses.html", expenses=expenses_list, partners=partners, month=month, editable_map=editable_map)

@app.route("/expenses/add", methods=["POST"])
@login_required
def expense_add():
    expense_date = request.form.get("expense_date")
    expense_type = request.form.get("expense_type")
    amount = float(request.form.get("amount") or 0)
    description = request.form.get("description", "").strip()
    partner_id = request.form.get("partner_id")

    if amount <= 0:
        flash("مبلغ باید بیشتر از صفر باشد.", "danger")
        return redirect(url_for("expenses"))

    allowed_types = ['مصرف مشترک', 'برداشت شخصی', 'مصرف شخصی قابل جبران', 'برداشت از دخل', 'مصرف از دخل']
    if expense_type not in allowed_types:
        flash("نوع مصرف نامعتبر است.", "danger")
        return redirect(url_for("expenses"))

    month = expense_date[:7] if expense_date else datetime.now().strftime("%Y-%m")

    # بررسی بسته بودن ماه
    conn = get_db()
    closed = conn.execute("SELECT id FROM monthly_closings WHERE month=?", (month,)).fetchone()
    if closed:
        conn.close()
        flash("این ماه بسته شده و قابل ثبت نیست.", "danger")
        return redirect(url_for("expenses"))

    if expense_type == "مصرف مشترک":
        partner_id = None
    else:
        if current_user.is_admin:
            partner_id = int(partner_id) if partner_id else None
        else:
            partner_id = current_user.id

    conn.execute("""
        INSERT INTO expenses (expense_date, month, partner_id, expense_type, amount, description, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (expense_date, month, partner_id, expense_type, amount, description, current_user.id))
    conn.commit()
    log_activity("ثبت مصرف/برداشت", f"{expense_type} - {amount}")
    conn.close()
    flash("ثبت شد.", "success")
    return redirect(url_for("expenses"))

@app.route("/expenses/edit/<int:id>", methods=["GET", "POST"])
@login_required
def expense_edit(id):
    conn = get_db()
    exp = conn.execute("SELECT * FROM expenses WHERE id=?", (id,)).fetchone()
    if not exp:
        flash("مورد یافت نشد.", "danger")
        conn.close()
        return redirect(url_for("expenses"))

    # فقط تا ۱ ساعت قابل ویرایش
    try:
        created = datetime.strptime(str(exp["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - created).total_seconds() > 3600 and not current_user.is_admin:
            flash("زمان ویرایش (۱ ساعت) به پایان رسیده است.", "danger")
            conn.close()
            return redirect(url_for("expenses"))
    except Exception:
        pass

    # فقط سازنده یا ادمین
    if not current_user.is_admin and exp["created_by"] != current_user.id:
        flash("فقط ثبت‌کننده می‌تواند ویرایش کند.", "danger")
        conn.close()
        return redirect(url_for("expenses"))

    if request.method == "POST":
        amount = float(request.form.get("amount") or 0)
        description = request.form.get("description", "").strip()
        expense_type = request.form.get("expense_type")
        if amount <= 0:
            flash("مبلغ نامعتبر.", "danger")
            conn.close()
            return redirect(url_for("expense_edit", id=id))
        conn.execute(
            "UPDATE expenses SET amount=?, description=?, expense_type=? WHERE id=?",
            (amount, description, expense_type, id)
        )
        conn.commit()
        log_activity("ویرایش مصرف", f"ID {id}")
        conn.close()
        flash("ویرایش شد.", "success")
        return redirect(url_for("expenses"))

    partners = conn.execute("SELECT id, full_name FROM users WHERE role='partner' AND is_active=1").fetchall()
    conn.close()
    return render_template("expense_edit.html", exp=exp, partners=partners)

# ---------- تقسیم سود ----------
@app.route("/profit")
@login_required
def profit():
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    conn = get_db()

    total_service = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE month = ?", (month,)
    ).fetchone()["t"]

    # مجموع اضافه‌کاری ماه
    total_overtime = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM overtime WHERE month = ?", (month,)
    ).fetchone()["t"]

    common_exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month = ? AND expense_type = 'مصرف مشترک'",
        (month,)
    ).fetchone()["t"]

    # همه مصرف‌های شخصی قابل جبران (از کل کم می‌شود)
    all_personal_exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month = ? AND expense_type = 'مصرف شخصی قابل جبران'",
        (month,)
    ).fetchone()["t"]

    # منطق جدید: درآمد سرویس + اضافه‌کاری - مصرف مشترک - همه مصرف شخصی قابل جبران
    net = total_service + total_overtime - common_exp - all_personal_exp
    base_share = net / 3 if net else 0

    partners = conn.execute(
        "SELECT id, full_name, partner_code FROM users WHERE role='partner' AND is_active=1 ORDER BY partner_code"
    ).fetchall()

    partner_details = []
    for p in partners:
        # برداشت شخصی + برداشت از دخل + مصرف از دخل → از حصه کم می‌شود
        withdrawals = conn.execute(
            """SELECT COALESCE(SUM(amount),0) as t FROM expenses
               WHERE month=? AND partner_id=? AND expense_type IN ('برداشت شخصی', 'برداشت از دخل', 'مصرف از دخل')""",
            (month, p["id"])
        ).fetchone()["t"]

        personal_exp = conn.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month=? AND partner_id=? AND expense_type='مصرف شخصی قابل جبران'",
            (month, p["id"])
        ).fetchone()["t"]

        # حصه نهایی = حصه پایه - برداشت‌ها + مصرف شخصی خودش
        final_share = base_share - withdrawals + personal_exp
        partner_details.append({
            "name": p["full_name"],
            "code": p["partner_code"],
            "base": base_share,
            "withdrawals": withdrawals,
            "personal_exp": personal_exp,
            "final": final_share
        })

    conn.close()
    return render_template("profit.html",
                           month=month,
                           total_service=total_service,
                           total_overtime=total_overtime,
                           common_exp=common_exp,
                           all_personal_exp=all_personal_exp,
                           net=net,
                           base_share=base_share,
                           partner_details=partner_details)

# ---------- لاگ فعالیت ----------
@app.route("/logs")
@login_required
@admin_required
def logs():
    conn = get_db()
    logs = conn.execute("""
        SELECT l.*, u.full_name
        FROM activity_log l
        LEFT JOIN users u ON l.user_id = u.id
        ORDER BY l.id DESC LIMIT 200
    """).fetchall()
    conn.close()
    return render_template("logs.html", logs=logs)

# ---------- تغییر رمز ----------
@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password")
        new_pass = request.form.get("new_password")
        confirm = request.form.get("confirm_password")

        if new_pass != confirm:
            flash("رمز جدید و تکرار آن یکسان نیست.", "danger")
            return redirect(url_for("change_password"))

        if len(new_pass) < 4:
            flash("رمز عبور باید حداقل ۴ کاراکتر باشد.", "danger")
            return redirect(url_for("change_password"))

        conn = get_db()
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (current_user.id,)).fetchone()
        if not check_password_hash(row["password_hash"], current):
            flash("رمز فعلی اشتباه است.", "danger")
            conn.close()
            return redirect(url_for("change_password"))

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_pass), current_user.id)
        )
        conn.commit()
        conn.close()
        log_activity("تغییر رمز عبور")
        flash("رمز عبور با موفقیت تغییر کرد.", "success")
        return redirect(url_for("dashboard"))

    return render_template("change_password.html")


# ---------- تنظیمات شرکت و لوگو ----------
@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    conn = get_db()
    if request.method == "POST":
        company_name = request.form.get("company_name", "شرکت اعتماد").strip()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('company_name', ?)", (company_name,))
        
        if 'logo' in request.files:
            file = request.files['logo']
            if file and file.filename and allowed_file(file.filename):
                from werkzeug.utils import secure_filename
                filename = "logo_" + secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('logo', ?)", (filename,))
        
        conn.commit()
        log_activity("تغییر تنظیمات شرکت")
        flash("تنظیمات ذخیره شد.", "success")
        conn.close()
        return redirect(url_for("settings"))
    
    company_name = conn.execute("SELECT value FROM settings WHERE key='company_name'").fetchone()
    logo = conn.execute("SELECT value FROM settings WHERE key='logo'").fetchone()
    conn.close()
    return render_template("settings.html",
                           company_name=company_name["value"] if company_name else "شرکت اعتماد",
                           logo=logo["value"] if logo else "")

# ---------- مدیریت شرکا (نام + عکس) ----------
@app.route("/partners")
@login_required
@admin_required
def partners():
    conn = get_db()
    partners_list = conn.execute(
        "SELECT * FROM users WHERE role='partner' ORDER BY partner_code"
    ).fetchall()
    conn.close()
    return render_template("partners.html", partners=partners_list)

@app.route("/partners/edit/<int:id>", methods=["GET", "POST"])
@login_required
@admin_required
def partner_edit(id):
    conn = get_db()
    partner = conn.execute("SELECT * FROM users WHERE id=? AND role='partner'", (id,)).fetchone()
    if not partner:
        flash("شریک یافت نشد.", "danger")
        conn.close()
        return redirect(url_for("partners"))
    
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        partner_code = request.form.get("partner_code", "").strip()
        
        photo_filename = partner["photo"]
        if 'photo' in request.files:
            file = request.files['photo']
            if file and file.filename and allowed_file(file.filename):
                from werkzeug.utils import secure_filename
                photo_filename = f"partner_{id}_" + secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], photo_filename)
                file.save(filepath)
        
        conn.execute(
            "UPDATE users SET full_name=?, partner_code=?, photo=? WHERE id=?",
            (full_name, partner_code, photo_filename, id)
        )
        conn.commit()
        log_activity("ویرایش شریک", full_name)
        flash("اطلاعات شریک به‌روزرسانی شد.", "success")
        conn.close()
        return redirect(url_for("partners"))
    
    conn.close()
    return render_template("partner_form.html", partner=partner)

# ---------- گزارش و چاپ برای هر شریک ----------
@app.route("/report/<int:partner_id>")
@login_required
def partner_report(partner_id):
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    conn = get_db()
    
    partner = conn.execute("SELECT * FROM users WHERE id=?", (partner_id,)).fetchone()
    if not partner:
        flash("شریک یافت نشد.", "danger")
        conn.close()
        return redirect(url_for("dashboard"))
    
    # فقط ادمین یا خود شریک می‌تواند ببیند
    if not current_user.is_admin and current_user.id != partner_id:
        flash("دسترسی غیرمجاز.", "danger")
        conn.close()
        return redirect(url_for("dashboard"))
    
    company_name = conn.execute("SELECT value FROM settings WHERE key='company_name'").fetchone()
    logo = conn.execute("SELECT value FROM settings WHERE key='logo'").fetchone()
    
    # فروش‌های این شریک در ماه
    sales = conn.execute("""
        SELECT s.*, p.name as product_name, p.code as product_code
        FROM sales s JOIN products p ON s.product_id = p.id
        WHERE s.seller_id=? AND s.month=?
        ORDER BY s.sale_date
    """, (partner_id, month)).fetchall()
    
    sales_total = conn.execute(
        "SELECT COALESCE(SUM(total_sale),0) as t, COALESCE(SUM(profit),0) as p FROM sales WHERE seller_id=? AND month=?",
        (partner_id, month)
    ).fetchone()
    
    # پرداخت‌های دریافتی این شریک
    payments = conn.execute("""
        SELECT pay.*, c.name as customer_name, c.code as customer_code
        FROM payments pay JOIN customers c ON pay.customer_id = c.id
        WHERE pay.received_by=? AND pay.month=?
        ORDER BY pay.payment_date
    """, (partner_id, month)).fetchall()
    
    payments_total = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE received_by=? AND month=?",
        (partner_id, month)
    ).fetchone()["t"]
    
    # مصارف و برداشت این شریک
    expenses = conn.execute("""
        SELECT * FROM expenses WHERE partner_id=? AND month=? ORDER BY expense_date
    """, (partner_id, month)).fetchall()
    
    # محاسبه حصه
    total_service = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE month=?", (month,)
    ).fetchone()["t"]
    common_exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month=? AND expense_type='مصرف مشترک'", (month,)
    ).fetchone()["t"]
    net = total_service - common_exp
    base_share = net / 3 if net else 0
    
    withdrawals = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month=? AND partner_id=? AND expense_type='برداشت شخصی'",
        (month, partner_id)
    ).fetchone()["t"]
    personal_exp = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month=? AND partner_id=? AND expense_type='مصرف شخصی قابل جبران'",
        (month, partner_id)
    ).fetchone()["t"]
    final_share = base_share - withdrawals + personal_exp
    
    conn.close()
    
    return render_template("partner_report.html",
                           partner=partner,
                           month=month,
                           company_name=company_name["value"] if company_name else "شرکت اعتماد",
                           logo=logo["value"] if logo else "",
                           sales=sales,
                           sales_total=sales_total,
                           payments=payments,
                           payments_total=payments_total,
                           expenses=expenses,
                           base_share=base_share,
                           withdrawals=withdrawals,
                           personal_exp=personal_exp,
                           final_share=final_share)

@app.route("/reports")
@login_required
def reports():
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    conn = get_db()
    partners_list = conn.execute(
        "SELECT id, full_name, partner_code, photo FROM users WHERE role='partner' AND is_active=1 ORDER BY partner_code"
    ).fetchall()
    conn.close()
    return render_template("reports.html", partners=partners_list, month=month)



# ---------- اضافه‌کاری ----------
@app.route("/overtime")
@login_required
def overtime():
    month = request.args.get("month", "")
    conn = get_db()
    query = """
        SELECT o.*, u.full_name as partner_name, creator.full_name as created_by_name
        FROM overtime o
        LEFT JOIN users u ON o.partner_id = u.id
        LEFT JOIN users creator ON o.created_by = creator.id
    """
    params = []
    if month:
        query += " WHERE o.month = ?"
        params.append(month)
    query += " ORDER BY o.overtime_date DESC, o.id DESC"
    items = conn.execute(query, params).fetchall()
    partners = conn.execute("SELECT id, full_name FROM users WHERE role='partner' AND is_active=1").fetchall()
    conn.close()
    return render_template("overtime.html", items=items, partners=partners, month=month)

@app.route("/overtime/add", methods=["POST"])
@login_required
def overtime_add():
    overtime_date = request.form.get("overtime_date")
    amount = float(request.form.get("amount") or 0)
    description = request.form.get("description", "").strip()
    partner_id = request.form.get("partner_id")

    if amount <= 0:
        flash("مبلغ باید بیشتر از صفر باشد.", "danger")
        return redirect(url_for("overtime"))

    month = overtime_date[:7] if overtime_date else datetime.now().strftime("%Y-%m")
    if current_user.is_admin:
        partner_id = int(partner_id) if partner_id else None
    else:
        partner_id = current_user.id

    conn = get_db()
    conn.execute("""
        INSERT INTO overtime (overtime_date, month, partner_id, amount, description, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (overtime_date, month, partner_id, amount, description, current_user.id))
    conn.commit()
    log_activity("ثبت اضافه‌کاری", f"{amount}")
    conn.close()
    flash("اضافه‌کاری ثبت شد.", "success")
    return redirect(url_for("overtime"))



# ---------- دخل (صندوق) ----------
@app.route("/cash")
@login_required
def cash():
    month = request.args.get("month", datetime.now().strftime("%Y-%m"))
    conn = get_db()

    # ورودی‌ها
    sales_in = conn.execute(
        "SELECT COALESCE(SUM(total_sale),0) as t FROM sales WHERE month=?", (month,)
    ).fetchone()["t"]
    service_in = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE month=?", (month,)
    ).fetchone()["t"]
    deposits = conn.execute(
        "SELECT COALESCE(SUM(amount),0) as t FROM cash_deposits WHERE month=?", (month,)
    ).fetchone()["t"]
    total_in = sales_in + service_in + deposits

    # خروجی‌ها از دخل
    cash_out = conn.execute(
        """SELECT COALESCE(SUM(amount),0) as t FROM expenses
           WHERE month=? AND expense_type IN ('برداشت از دخل', 'مصرف از دخل')""",
        (month,)
    ).fetchone()["t"]

    balance = total_in - cash_out

    deposit_list = conn.execute("""
        SELECT d.*, u.full_name as created_by_name
        FROM cash_deposits d LEFT JOIN users u ON d.created_by = u.id
        WHERE d.month=? ORDER BY d.deposit_date DESC
    """, (month,)).fetchall()

    out_list = conn.execute("""
        SELECT e.*, u.full_name as partner_name
        FROM expenses e LEFT JOIN users u ON e.partner_id = u.id
        WHERE e.month=? AND e.expense_type IN ('برداشت از دخل', 'مصرف از دخل')
        ORDER BY e.expense_date DESC
    """, (month,)).fetchall()

    conn.close()
    return render_template("cash.html",
                           month=month,
                           sales_in=sales_in,
                           service_in=service_in,
                           deposits=deposits,
                           total_in=total_in,
                           cash_out=cash_out,
                           balance=balance,
                           deposit_list=deposit_list,
                           out_list=out_list)

@app.route("/cash/deposit", methods=["POST"])
@login_required
@admin_required
def cash_deposit():
    deposit_date = request.form.get("deposit_date")
    amount = float(request.form.get("amount") or 0)
    description = request.form.get("description", "").strip()
    if amount <= 0:
        flash("مبلغ نامعتبر.", "danger")
        return redirect(url_for("cash"))
    month = deposit_date[:7] if deposit_date else datetime.now().strftime("%Y-%m")
    conn = get_db()
    closed = conn.execute("SELECT id FROM monthly_closings WHERE month=?", (month,)).fetchone()
    if closed:
        conn.close()
        flash("این ماه بسته شده است.", "danger")
        return redirect(url_for("cash"))
    conn.execute(
        "INSERT INTO cash_deposits (deposit_date, month, amount, description, created_by) VALUES (?, ?, ?, ?, ?)",
        (deposit_date, month, amount, description, current_user.id)
    )
    conn.commit()
    log_activity("واریز به دخل", f"{amount}")
    conn.close()
    flash("واریز به دخل ثبت شد.", "success")
    return redirect(url_for("cash", month=month))

# ---------- ثبت ماهانه ----------
@app.route("/monthly")
@login_required
def monthly():
    conn = get_db()
    closings = conn.execute("""
        SELECT m.*, u.full_name as closed_by_name
        FROM monthly_closings m LEFT JOIN users u ON m.closed_by = u.id
        ORDER BY m.month DESC
    """).fetchall()
    conn.close()
    return render_template("monthly.html", closings=closings)

@app.route("/monthly/close", methods=["POST"])
@login_required
@admin_required
def monthly_close():
    month = request.form.get("month", "").strip()
    notes = request.form.get("notes", "").strip()
    if not month:
        flash("ماه را انتخاب کنید.", "danger")
        return redirect(url_for("monthly"))

    conn = get_db()
    exists = conn.execute("SELECT id FROM monthly_closings WHERE month=?", (month,)).fetchone()
    if exists:
        conn.close()
        flash("این ماه قبلاً بسته شده است.", "warning")
        return redirect(url_for("monthly"))

    total_sales = conn.execute("SELECT COALESCE(SUM(total_sale),0) as t FROM sales WHERE month=?", (month,)).fetchone()["t"]
    total_sales_profit = conn.execute("SELECT COALESCE(SUM(profit),0) as t FROM sales WHERE month=?", (month,)).fetchone()["t"]
    total_service = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM payments WHERE month=?", (month,)).fetchone()["t"]
    total_overtime = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM overtime WHERE month=?", (month,)).fetchone()["t"]
    total_common_exp = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month=? AND expense_type='مصرف مشترک'", (month,)).fetchone()["t"]
    total_personal_exp = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM expenses WHERE month=? AND expense_type='مصرف شخصی قابل جبران'", (month,)).fetchone()["t"]
    total_withdrawals = conn.execute(
        """SELECT COALESCE(SUM(amount),0) as t FROM expenses
           WHERE month=? AND expense_type IN ('برداشت شخصی','برداشت از دخل','مصرف از دخل')""", (month,)
    ).fetchone()["t"]
    deposits = conn.execute("SELECT COALESCE(SUM(amount),0) as t FROM cash_deposits WHERE month=?", (month,)).fetchone()["t"]
    cash_out = conn.execute(
        """SELECT COALESCE(SUM(amount),0) as t FROM expenses
           WHERE month=? AND expense_type IN ('برداشت از دخل','مصرف از دخل')""", (month,)
    ).fetchone()["t"]
    total_cash_in = total_sales + total_service + deposits
    cash_balance = total_cash_in - cash_out

    conn.execute("""
        INSERT INTO monthly_closings
        (month, closed_at, closed_by, total_sales, total_sales_profit, total_service, total_overtime,
         total_common_exp, total_personal_exp, total_withdrawals, total_cash_in, total_cash_out, cash_balance, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (month, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), current_user.id,
          total_sales, total_sales_profit, total_service, total_overtime,
          total_common_exp, total_personal_exp, total_withdrawals, total_cash_in, cash_out, cash_balance, notes))
    conn.commit()
    log_activity("بستن ماه", month)
    conn.close()
    flash(f"ماه {month} با موفقیت بسته و ثبت شد.", "success")
    return redirect(url_for("monthly"))

@app.route("/monthly/view/<month>")
@login_required
def monthly_view(month):
    conn = get_db()
    closing = conn.execute("SELECT m.*, u.full_name as closed_by_name FROM monthly_closings m LEFT JOIN users u ON m.closed_by=u.id WHERE m.month=?", (month,)).fetchone()
    conn.close()
    if not closing:
        flash("ثبت ماهانه یافت نشد.", "danger")
        return redirect(url_for("monthly"))
    return render_template("monthly_view.html", c=closing)


# ==================== Main ====================

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print("سیستم مدیریت شراکت آماده است")
    print(f"آدرس: http://0.0.0.0:{port}")
    if USE_TURSO:
        print("دیتابیس: Turso (ابری - دائمی)")
    else:
        print("دیتابیس: SQLite محلی")
    print("-" * 50)
    print("کاربران پیش‌فرض:")
    print("  ادمین   → username: admin    password: admin123")
    print("  شریک ۱  → username: partner1 password: p1123")
    print("  شریک ۲  → username: partner2 password: p2123")
    print("  شریک ۳  → username: partner3 password: p3123")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False)
