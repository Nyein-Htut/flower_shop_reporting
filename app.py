from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file, g
from sqlalchemy.orm import joinedload
from sqlalchemy import text
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, timezone
from functools import wraps
import os
from dotenv import load_dotenv
load_dotenv()
import csv
import io
import cloudinary
import cloudinary.uploader

app = Flask(__name__)

# ==========================================
# CONFIGURATION
# ==========================================
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key")

app.config.update(
    SESSION_PERMANENT=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_REFRESH_EACH_REQUEST=True,
    MAX_CONTENT_LENGTH=40 * 1024 * 1024  # several phone photos per order
)

# Cloudinary reads CLOUDINARY_URL (cloudinary://key:secret@cloud_name) from the environment.
cloudinary.config(secure=True)

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', '')

if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 10,
    'pool_size': 5,
    'max_overflow': 10
}

# Who can receive money / how it was paid (used by the paid tick on each order
# and by the expense page to be built later).
PAYMENT_RECEIVERS = ['Ko Kyaw', 'Iris', 'Other']
PAYMENT_METHODS = ['KPay', 'Cash']

def get_myanmar_now():
    """Returns a datetime object set explicitly to Myanmar Time (UTC +6:30)"""
    myanmar_tz = timezone(timedelta(hours=6, minutes=30))
    return datetime.now(myanmar_tz)

db = SQLAlchemy(app)

@app.template_filter('thumb')
def cloud_thumb(url, size=480):
    """Cloudinary URL -> small square auto-compressed thumbnail (fast lazy lists).
    The full-size URL is still used when a photo is tapped."""
    if url and 'res.cloudinary.com' in url and '/upload/' in url:
        return url.replace('/upload/', f'/upload/c_fill,g_auto,w_{size},h_{size},q_auto,f_auto/', 1)
    return url

# ==========================================
# DATABASE MODELS
# ==========================================
class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(50), nullable=False, default=lambda: get_myanmar_now().strftime('%Y-%m-%d'))
    source = db.Column(db.String(100), default='-')
    customer = db.Column(db.String(100), nullable=False)
    total_price = db.Column(db.Integer, nullable=False, default=0)
    time = db.Column(db.String(50), default='-')
    address = db.Column(db.Text, default='-')
    # Legacy order-level wrapper. Wrapper is now chosen per bouquet (OrderItem.wrapped_by);
    # this column is kept so old orders still display.
    wrapped_by = db.Column(db.Text, default='')
    delivery_fee = db.Column(db.Integer, default=0)  # ကားခ / 车费
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    payment_date = db.Column(db.String(50), default='')
    received_by = db.Column(db.String(100), default='')      # Ko Kyaw / Iris / Other
    payment_method = db.Column(db.String(50), default='')    # KPay / Cash

    items = db.relationship(
        'OrderItem',
        backref='order',
        cascade="all, delete-orphan",
        lazy='selectin'
    )

    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date,
            'source': self.source,
            'customer': self.customer,
            'time': self.time,
            'address': self.address,
            'delivery_fee': self.delivery_fee or 0,
            'items': [it.to_dict() for it in self.items],
        }

class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    item_name = db.Column(db.Text, default='Flower Bouquet')
    price = db.Column(db.Integer, default=0)
    remarks = db.Column(db.Text, default='-')
    paper_color = db.Column(db.Text, default='')        # Paper အရောင် / 包装纸颜色
    photo_url = db.Column(db.Text, default='')          # bouquet photo (Cloudinary)
    wrapped_by = db.Column(db.Text, default='')         # ပန်းစည်းစည်းသူ / 包花员工 (name snapshot)
    has_cake = db.Column(db.Boolean, default=False)     # ကိတ်ပါ မပါ
    cake_photo_url = db.Column(db.Text, default='')     # cake photo (Cloudinary)

    def to_dict(self):
        return {
            'item_name': self.item_name or '',
            'price': self.price or 0,
            'remarks': '' if self.remarks == '-' else (self.remarks or ''),
            'paper_color': self.paper_color or '',
            'photo_url': self.photo_url or '',
            'wrapped_by': self.wrapped_by or '',
            'has_cake': bool(self.has_cake),
            'cake_photo_url': self.cake_photo_url or '',
        }

class Wrapper(db.Model):
    """Staff who wrap bouquets. Managed by the manager (add / edit / delete)."""
    __tablename__ = 'wrappers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)

def ensure_schema():
    """Creates the wrappers table and adds the new columns on existing databases.
    Safe to run on every startup (IF NOT EXISTS)."""
    statements = [
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_fee INTEGER DEFAULT 0",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS received_by VARCHAR(100) DEFAULT ''",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50) DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS paper_color TEXT DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS photo_url TEXT DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS wrapped_by TEXT DEFAULT ''",
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS has_cake BOOLEAN DEFAULT FALSE",
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS cake_photo_url TEXT DEFAULT ''",
    ]
    try:
        with app.app_context():
            db.create_all()
            with db.engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
    except Exception as e:
        print("ENSURE SCHEMA ERROR:", e)

ensure_schema()

# ==========================================
# AUTHENTICATION DECORATORS
# ==========================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        if session.get('role') != 'manager':
            return redirect(url_for('staff_view'))
        return f(*args, **kwargs)
    return decorated_function

def _is_manager():
    return session.get('role') == 'manager'

def _role_home_redirect(order_date=None, order_id=None):
    """Send the user back to whichever daily view matches their role.
    After add/edit we jump to that order's day and highlight its card,
    so the saved order is always visible (no hidden-by-date surprises)."""
    endpoint = 'staff_view' if session.get('role') == 'staff' else 'index'
    if order_date and order_id:
        return redirect(url_for(endpoint, view='day', day=order_date, highlight=order_id))
    return redirect(url_for(endpoint))

# ==========================================
# AUTHENTICATION ROUTES
# ==========================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    portal = request.form.get('portal', 'manager') if request.method == 'POST' else request.args.get('portal', 'manager')

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        portal = request.form.get('portal', 'manager')

        if portal == 'staff':
            if username == "Staff" and password == os.environ.get("STAFF_PASSWORD", ""):
                session.clear()
                session['logged_in'] = True
                session['role'] = 'staff'
                session.permanent = False
                return redirect(url_for('staff_view'))
            error = "员工凭证错误，请重新输入 (Invalid staff credentials)"
            portal = 'staff'
        elif username == "Iris Flower" and password == os.environ.get("MANAGER_PASSWORD", ""):
            session.clear()
            session['logged_in'] = True
            session['role'] = 'manager'
            session.permanent = False
            return redirect(url_for('index'))
        else:
            error = "管理员凭证错误，请重新输入 (Invalid manager credentials)"

    return render_template('login.html', error=error, portal=portal)

@app.route('/logout')
def logout():
    session.clear()
    response = redirect(url_for('login'))
    response.delete_cookie('session')
    return response

# ==========================================
# HELPERS
# ==========================================
@app.before_request
def refresh_session():
    if session.get('logged_in'):
        session.modified = True

def _parse_daily_filters(default_view='day'):
    if default_view not in ('day', 'month', 'year'):
        default_view = 'day'

    view_mode = request.args.get('view', default_view)
    if view_mode not in ('day', 'month', 'year'):
        view_mode = default_view

    selected_day = (request.args.get('day') or '').strip() or get_myanmar_now().strftime('%Y-%m-%d')
    selected_month = (request.args.get('month') or '').strip() or get_myanmar_now().strftime('%Y-%m')
    selected_year = (request.args.get('year') or '').strip() or get_myanmar_now().strftime('%Y')

    filter_value = {'day': selected_day, 'month': selected_month, 'year': selected_year}[view_mode]
    return view_mode, filter_value, selected_day, selected_month, selected_year

def _format_date_display(date_str):
    try:
        d = datetime.strptime(date_str, '%Y-%m-%d')
        weekdays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        return f"{d.strftime('%B %d, %Y')} ({weekdays[d.weekday()]})"
    except ValueError:
        return date_str

def _time_sort_key(order):
    t = (order.time or '').strip()
    for fmt in ('%H:%M', '%I:%M %p', '%I:%M%p', '%H:%M:%S'):
        try:
            parsed = datetime.strptime(t, fmt)
            return parsed.hour * 60 + parsed.minute
        except ValueError:
            continue
    return 24 * 60  # unparseable/blank times sort last

def _group_query_by_day(query):
    orders = query.order_by(Order.date.desc(), Order.id.desc()).all()

    groups = {}
    for order in orders:
        groups.setdefault(order.date, []).append(order)

    orders_by_day = []
    for date in sorted(groups.keys(), reverse=True):
        day_orders = sorted(groups[date], key=_time_sort_key)
        orders_by_day.append({
            'date': date,
            'date_display': _format_date_display(date),
            'orders': day_orders,
            'order_count': len(day_orders),
            'day_total': sum(o.total_price for o in day_orders),
        })

    return orders_by_day

def _fetch_orders_for_period(mode, value, source=None, min_price=None, max_price=None, paid=None):
    """Fetches orders scoped to a single day, a whole month, or a whole
    year, grouped by day. Optionally restricted to a source, a total-price
    range and/or paid ('paid') / unpaid ('unpaid') status."""
    query = Order.query.options(joinedload(Order.items))

    if mode == 'day' and value:
        query = query.filter(Order.date == value)
    elif mode in ('month', 'year') and value:
        query = query.filter(Order.date.like(f"{value}%"))
    else:
        cutoff = (get_myanmar_now() - timedelta(days=30)).strftime('%Y-%m-%d')
        query = query.filter(Order.date >= cutoff)

    if source:
        query = query.filter(Order.source == source)

    if min_price is not None:
        query = query.filter(Order.total_price >= min_price)
    if max_price is not None:
        query = query.filter(Order.total_price <= max_price)

    if paid == 'paid':
        query = query.filter(Order.is_paid.is_(True))
    elif paid == 'unpaid':
        query = query.filter(Order.is_paid.is_(False))

    return _group_query_by_day(query)

_fetch_orders_grouped_by_day = _fetch_orders_for_period
_fetch_orders_for_export = _fetch_orders_for_period

def _available_years():
    years = sorted({
        r[0][:4]
        for r in Order.query.with_entities(Order.date).distinct().all()
        if r[0] and len(r[0]) >= 4
    }, reverse=True)
    return years or [get_myanmar_now().strftime('%Y')]

PRICE_FILTER_MIN = 0
PRICE_FILTER_MAX = 5000000
DAY_CHUNK = 4   # day-groups rendered per batch (lazy / infinite scroll)

def _parse_price_range():
    """Optional price-range filter (0 - 5,000,000 MMK). The UI slider was
    removed, but the query-string params still work. Only applied when
    narrower than the full range."""
    try:
        raw_min = request.args.get('price_min')
        min_price = int(raw_min) if raw_min not in (None, '') else PRICE_FILTER_MIN
    except (TypeError, ValueError):
        min_price = PRICE_FILTER_MIN
    try:
        raw_max = request.args.get('price_max')
        max_price = int(raw_max) if raw_max not in (None, '') else PRICE_FILTER_MAX
    except (TypeError, ValueError):
        max_price = PRICE_FILTER_MAX

    min_price = max(PRICE_FILTER_MIN, min(min_price, PRICE_FILTER_MAX))
    max_price = max(PRICE_FILTER_MIN, min(max_price, PRICE_FILTER_MAX))
    if min_price > max_price:
        min_price, max_price = max_price, min_price

    applied_min = min_price if min_price > PRICE_FILTER_MIN else None
    applied_max = max_price if max_price < PRICE_FILTER_MAX else None
    return min_price, max_price, applied_min, applied_max

def _parse_paid_filter():
    paid = (request.args.get('paid') or '').strip()
    return paid if paid in ('paid', 'unpaid') else ''

def _wrapper_list():
    try:
        return [{'id': w.id, 'name': w.name} for w in Wrapper.query.order_by(Wrapper.name).all()]
    except Exception as e:
        db.session.rollback()
        print("WRAPPER LIST ERROR:", e)
        return []

def _daily_view_context(filter_action, default_view='day'):
    view_mode, filter_value, selected_day, selected_month, selected_year = _parse_daily_filters(default_view)
    selected_source = (request.args.get('source') or '').strip()
    selected_paid = _parse_paid_filter()
    price_min, price_max, applied_min, applied_max = _parse_price_range()

    orders_by_day = _fetch_orders_for_period(
        view_mode, filter_value, source=selected_source or None,
        min_price=applied_min, max_price=applied_max, paid=selected_paid or None
    )
    total_orders = sum(d['order_count'] for d in orders_by_day)
    total_revenue = sum(d['day_total'] for d in orders_by_day)
    day_count = len(orders_by_day)

    return {
        'orders_by_day': orders_by_day[:DAY_CHUNK],
        'day_count': day_count,
        'has_more': day_count > DAY_CHUNK,
        'next_offset': DAY_CHUNK,
        'highlight_id': _to_int(request.args.get('highlight'), 0),
        'view_mode': view_mode,
        'selected_day': selected_day,
        'selected_month': selected_month,
        'selected_year': selected_year,
        'selected_source': selected_source,
        'selected_paid': selected_paid,
        'price_min': price_min,
        'price_max': price_max,
        'price_filter_min': PRICE_FILTER_MIN,
        'price_filter_max': PRICE_FILTER_MAX,
        'available_years': _available_years(),
        'filter_action': filter_action,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'wrappers': _wrapper_list(),
    }

def _serialize_order_for_export(o):
    return {
        'id': o.id,
        'date': o.date,
        'customer': o.customer,
        'source': o.source,
        'time': o.time,
        'address': o.address,
        'wrapped_by': o.wrapped_by or '',
        'delivery_fee': o.delivery_fee or 0,
        'total_price': o.total_price,
        'is_paid': bool(o.is_paid),
        'payment_date': o.payment_date or '',
        'received_by': o.received_by or '',
        'payment_method': o.payment_method or '',
        'items': [
            {
                'item_name': it.item_name,
                'price': it.price,
                'remarks': it.remarks,
                'paper_color': it.paper_color or '',
                'wrapped_by': it.wrapped_by or '',
                'has_cake': bool(it.has_cake),
            }
            for it in o.items
        ],
    }

# ---------- order form helpers (add + edit share these) ----------
def _to_int(value, default=0):
    try:
        if value in (None, ''):
            return default
        return int(str(value).replace(',', '').strip())
    except (ValueError, TypeError):
        return default

def _upload_image(file_storage):
    """Uploads one image to Cloudinary and returns its https URL (or None)."""
    if not file_storage or not file_storage.filename:
        return None
    try:
        # Stored copy is capped at 1600px and auto-compressed (the browser also
        # shrinks photos before upload, this is the safety net).
        result = cloudinary.uploader.upload(
            file_storage.stream, folder="iris_flower", resource_type="image",
            transformation=[{"width": 1600, "height": 1600, "crop": "limit"}, {"quality": "auto"}]
        )
        return result.get('secure_url')
    except Exception as e:
        print("CLOUDINARY UPLOAD ERROR:", e)
        g.upload_error = str(e)[:160]
        return None

def _apply_items(order):
    """Reads the bouquet sub-forms from request.form / request.files, replaces
    order.items with them and returns (items_total, failed_upload_count).
    Rows are matched by index; existing_* hidden fields keep old photos on edit."""
    form, files = request.form, request.files

    names = form.getlist('item_name[]')
    colors = form.getlist('paper_color[]')
    prices = form.getlist('item_price[]')
    wrappers = form.getlist('item_wrapped_by[]')
    remarks = form.getlist('remarks[]')
    cakes = form.getlist('has_cake[]')
    old_photos = form.getlist('existing_photo[]')
    old_cake_photos = form.getlist('existing_cake_photo[]')
    photos = files.getlist('item_photo[]')
    cake_photos = files.getlist('cake_photo[]')

    def at(lst, i, default=''):
        return lst[i] if i < len(lst) else default

    new_items, total, failed = [], 0, 0
    for i, name in enumerate(names):
        if not (name or '').strip():
            continue

        price = _to_int(at(prices, i))
        total += price

        photo_url = at(old_photos, i)
        new_photo = at(photos, i, None)
        if new_photo and new_photo.filename:
            uploaded = _upload_image(new_photo)
            if uploaded:
                photo_url = uploaded
            else:
                failed += 1

        has_cake = at(cakes, i, '0') == '1'
        cake_photo_url = ''
        if has_cake:
            cake_photo_url = at(old_cake_photos, i)
            new_cake = at(cake_photos, i, None)
            if new_cake and new_cake.filename:
                uploaded = _upload_image(new_cake)
                if uploaded:
                    cake_photo_url = uploaded
                else:
                    failed += 1

        new_items.append(OrderItem(
            item_name=name.strip(),
            price=price,
            remarks=(at(remarks, i) or '').strip() or '-',
            paper_color=(at(colors, i) or '').strip(),
            photo_url=photo_url or '',
            wrapped_by=(at(wrappers, i) or '').strip(),
            has_cake=has_cake,
            cake_photo_url=cake_photo_url or '',
        ))

    order.items = new_items
    return total, failed

# ==========================================
# MAIN ROUTES
# ==========================================
@app.route('/')
@manager_required
def index():
    db.session.remove()
    ctx = _daily_view_context(url_for('index'), default_view='month')

    if ctx['view_mode'] == 'day':
        export_params = {'day': ctx['selected_day']}
        period_value = ctx['selected_day']
    elif ctx['view_mode'] == 'month':
        export_params = {'month': ctx['selected_month']}
        period_value = ctx['selected_month']
    else:
        export_params = {'year': ctx['selected_year']}
        period_value = ctx['selected_year']

    if ctx.get('selected_source'):
        export_params['source'] = ctx['selected_source']
    if ctx.get('selected_paid'):
        export_params['paid'] = ctx['selected_paid']
    if ctx.get('price_min', PRICE_FILTER_MIN) > PRICE_FILTER_MIN:
        export_params['price_min'] = ctx['price_min']
    if ctx.get('price_max', PRICE_FILTER_MAX) < PRICE_FILTER_MAX:
        export_params['price_max'] = ctx['price_max']

    return render_template(
        'daily.html', active_page='daily', readonly=False, show_payment=True, can_mark_paid=True,
        export_params=export_params, period_value=period_value, **ctx
    )

@app.route('/staff')
@login_required
def staff_view():
    if session.get('role') == 'manager':
        return redirect(url_for('index'))
    db.session.remove()
    ctx = _daily_view_context(url_for('staff_view'))
    return render_template(
        'staff_daily.html', active_page='staff', readonly=True,
        show_payment=False, can_mark_paid=True, **ctx
    )

@app.route('/api/daily_chunk')
@login_required
def api_daily_chunk():
    """Next batch of day groups for lazy / infinite scroll (same filters as the page)."""
    is_staff = session.get('role') == 'staff'
    view_mode, filter_value, *_ = _parse_daily_filters('day' if is_staff else 'month')
    source = (request.args.get('source') or '').strip()
    paid = _parse_paid_filter()
    _, _, applied_min, applied_max = _parse_price_range()

    groups = _fetch_orders_for_period(
        view_mode, filter_value, source=source or None,
        min_price=applied_min, max_price=applied_max, paid=paid or None
    )
    offset = max(0, _to_int(request.args.get('offset'), 0))
    chunk = groups[offset:offset + DAY_CHUNK]

    html = render_template(
        'partials/daily_records_groups.html',
        orders_by_day=chunk, readonly=is_staff, show_payment=not is_staff, can_mark_paid=True
    )
    db.session.remove()
    end = offset + len(chunk)

    export_params = {view_mode: filter_value}
    if source:
        export_params['source'] = source
    if paid:
        export_params['paid'] = paid

    return jsonify({
        'html': html,
        'next_offset': end,
        'has_more': end < len(groups),
        'total_orders': sum(g['order_count'] for g in groups),
        'total_revenue': sum(g['day_total'] for g in groups),
        'day_count': len(groups),
        'export_params': export_params,
        'period_value': filter_value,
    })

@app.route('/api/export_orders')
@manager_required
def api_export_orders():
    db.session.remove()

    day = (request.args.get('day') or '').strip()
    month = (request.args.get('month') or '').strip()
    year = (request.args.get('year') or '').strip()
    view = (request.args.get('view') or '').strip()
    source = (request.args.get('source') or '').strip()
    paid = _parse_paid_filter()
    _, _, applied_min, applied_max = _parse_price_range()

    if day:
        mode, value = 'day', day
    elif month:
        mode, value = 'month', month
    elif year or view == 'year':
        mode, value = 'year', (year or get_myanmar_now().strftime('%Y'))
    else:
        mode, value = 'all', ''

    groups = _fetch_orders_for_period(
        mode, value, source=source or None,
        min_price=applied_min, max_price=applied_max, paid=paid or None
    )
    total_orders = sum(g['order_count'] for g in groups)
    total_revenue = sum(g['day_total'] for g in groups)

    payload = {
        'mode': mode,
        'value': value,
        'source': source,
        'paid': paid,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
        'orders_by_day': [
            {
                'date': g['date'],
                'date_display': g['date_display'],
                'order_count': g['order_count'],
                'day_total': g['day_total'],
                'orders': [_serialize_order_for_export(o) for o in g['orders']],
            }
            for g in groups
        ],
    }
    db.session.remove()
    return jsonify(payload)

@app.route('/add_order', methods=['POST'])
@login_required
def add_order():
    # Payment is ticked on the order card afterwards (needs receiver + method),
    # so new orders always start unpaid.
    new_order = Order(
        date=request.form.get('date') or get_myanmar_now().strftime('%Y-%m-%d'),
        source=request.form.get('source') or '-',
        customer=(request.form.get('customer') or '').strip() or '-',
        total_price=0,
        time=request.form.get('time') or '-',
        address=(request.form.get('address') or '').strip() or '-',
        wrapped_by='',
        delivery_fee=_to_int(request.form.get('delivery_fee')),
        is_paid=False,
        payment_date='',
        received_by='',
        payment_method='',
    )

    saved_id = saved_date = None
    try:
        items_total, failed = _apply_items(new_order)
        # Delivery fee (ကားခ) is stored separately and NOT added to total_price,
        # so revenue reports stay unchanged. To include it: items_total + new_order.delivery_fee
        new_order.total_price = items_total
        db.session.add(new_order)
        db.session.commit()
        saved_id, saved_date = new_order.id, new_order.date
        if failed:
            flash("Order saved, but some photos failed to upload: " + getattr(g, 'upload_error', 'unknown error'), 'error')
    except Exception as e:
        db.session.rollback()
        print("ADD ORDER ERROR:", e)
        flash("Failed to save order. Please try again.", 'error')
    finally:
        db.session.remove()

    return _role_home_redirect(saved_date, saved_id)

@app.route('/delete_order/<int:id>', methods=['GET', 'POST'])
@login_required
def delete_order(id):
    order = Order.query.options(joinedload(Order.items)).get_or_404(id)
    try:
        db.session.delete(order)
        db.session.commit()
        flash("Order deleted successfully.")
    except Exception as e:
        db.session.rollback()
        print("DELETE ERROR:", e)
        flash("Failed to delete order. Please try again.")
    finally:
        db.session.remove()
    return _role_home_redirect()

@app.route('/edit_order/<int:order_id>', methods=['POST'])
@login_required
def edit_order(order_id):
    order = Order.query.get_or_404(order_id)
    order.date = request.form.get('date') or order.date
    order.source = request.form.get('source') or '-'
    order.customer = (request.form.get('customer') or '').strip() or '-'
    order.time = request.form.get('time') or '-'
    order.address = (request.form.get('address') or '').strip() or '-'
    order.delivery_fee = _to_int(request.form.get('delivery_fee'))

    saved_id = saved_date = None
    try:
        items_total, failed = _apply_items(order)
        order.total_price = items_total
        db.session.commit()
        saved_id, saved_date = order.id, order.date
        if failed:
            flash("Order updated, but some photos failed to upload: " + getattr(g, 'upload_error', 'unknown error'), 'error')
        else:
            flash('Order updated successfully.')
    except Exception as e:
        db.session.rollback()
        print("EDIT ERROR:", e)
        flash("Failed to update order. Please try again.", 'error')
    finally:
        db.session.remove()

    return _role_home_redirect(saved_date, saved_id)

# ==========================================
# WRAPPER (ပန်းစည်းစည်းသူ / 包花员工) MANAGEMENT
# ==========================================
@app.route('/api/wrappers', methods=['GET'])
@login_required
def wrappers_list():
    data = _wrapper_list()
    db.session.remove()
    return jsonify({'success': True, 'wrappers': data})

@app.route('/api/wrappers', methods=['POST'])
@manager_required
def wrappers_add():
    name = ((request.get_json(silent=True) or {}).get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name is required'}), 400
    try:
        if Wrapper.query.filter(db.func.lower(Wrapper.name) == name.lower()).first():
            return jsonify({'success': False, 'error': 'Already exists'}), 409
        db.session.add(Wrapper(name=name))
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        print("WRAPPER ADD ERROR:", e)
        return jsonify({'success': False, 'error': 'Save failed'}), 500
    finally:
        db.session.remove()

@app.route('/api/wrappers/<int:wrapper_id>', methods=['PUT'])
@manager_required
def wrappers_edit(wrapper_id):
    name = ((request.get_json(silent=True) or {}).get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name is required'}), 400
    try:
        w = Wrapper.query.get_or_404(wrapper_id)
        clash = Wrapper.query.filter(db.func.lower(Wrapper.name) == name.lower(), Wrapper.id != wrapper_id).first()
        if clash:
            return jsonify({'success': False, 'error': 'Already exists'}), 409
        w.name = name
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        print("WRAPPER EDIT ERROR:", e)
        return jsonify({'success': False, 'error': 'Save failed'}), 500
    finally:
        db.session.remove()

@app.route('/api/wrappers/<int:wrapper_id>', methods=['DELETE'])
@manager_required
def wrappers_delete(wrapper_id):
    try:
        w = Wrapper.query.get_or_404(wrapper_id)
        db.session.delete(w)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        print("WRAPPER DELETE ERROR:", e)
        return jsonify({'success': False, 'error': 'Delete failed'}), 500
    finally:
        db.session.remove()

# ==========================================
# REPORTING
# ==========================================
def _normalize_source(source):
    source = (source or '').strip()
    if source in ['', '-']:
        return 'Other'
    return source

def _compute_monthly_report(view_mode, selected_month, selected_year):
    if view_mode == 'year':
        date_filter = f"{selected_year}%"
        period_label = selected_year
    else:
        date_filter = f"{selected_month}%"
        period_label = selected_month

    monthly_orders = (
        Order.query
        .options(joinedload(Order.items))
        .filter(Order.date.like(date_filter))
        .all()
    )

    total_revenue = sum(order.total_price for order in monthly_orders)
    total_orders = len(monthly_orders)
    avg_ticket = total_revenue / total_orders if total_orders else 0
    unique_customers = len(set(o.customer for o in monthly_orders if o.customer))

    source_revenue = {}
    for order in monthly_orders:
        source = _normalize_source(order.source)
        source_revenue[source] = source_revenue.get(source, 0) + order.total_price

    channels = []
    for source, revenue in source_revenue.items():
        percentage = round((revenue / total_revenue) * 100) if total_revenue else 0
        channels.append({'name': source, 'revenue': revenue, 'percentage': percentage})
    channels.sort(key=lambda x: x['revenue'], reverse=True)

    item_stats = {}
    total_items_count = 0
    for order in monthly_orders:
        for item in order.items:
            total_items_count += 1
            item_name = item.item_name or 'Unknown'
            price = item.price or 0
            if item_name not in item_stats:
                item_stats[item_name] = {'count': 0, 'revenue': 0}
            item_stats[item_name]['count'] += 1
            item_stats[item_name]['revenue'] += price

    top_items = []
    for name, stats in item_stats.items():
        top_items.append({
            'name': name,
            'count': stats['count'],
            'revenue': stats['revenue']
        })
    top_items.sort(key=lambda x: x['count'], reverse=True)
    top_items_all = top_items
    top_items = top_items[:10]

    profit_margin = 58
    estimated_profit = int(total_revenue * profit_margin / 100)
    items_per_order = round(total_items_count / total_orders, 1) if total_orders else 0
    canceled_orders = sum(1 for o in monthly_orders if o.total_price == 0)
    refund_rate = round(canceled_orders / total_orders * 100, 1) if total_orders else 0

    if view_mode == 'year':
        trend_labels = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        trend_data = [0] * 12
        for order in monthly_orders:
            try:
                m = int(order.date.split('-')[1]) - 1
                if 0 <= m < 12:
                    trend_data[m] += order.total_price
            except (ValueError, IndexError):
                pass
        num_periods = 12
    else:
        year, month = map(int, selected_month.split('-'))
        if month == 12:
            num_days = (datetime(year + 1, 1, 1) - datetime(year, month, 1)).days
        else:
            num_days = (datetime(year, month + 1, 1) - datetime(year, month, 1)).days

        daily_map = {f"{selected_month}-{str(day).zfill(2)}": 0 for day in range(1, num_days + 1)}
        for order in monthly_orders:
            if order.date in daily_map:
                daily_map[order.date] += order.total_price

        trend_labels = [f"{i}" for i in range(1, num_days + 1)]
        trend_data = [daily_map[f"{selected_month}-{str(i).zfill(2)}"] for i in range(1, num_days + 1)]
        num_periods = num_days

    if view_mode == 'year':
        past_filter = Order.date < f"{selected_year}-01-01"
    else:
        past_filter = Order.date < f"{selected_month}-01"

    past_customers = set(
        r[0].strip()
        for r in Order.query.filter(past_filter).with_entities(Order.customer).all()
        if r[0]
    )
    current_customers = set(o.customer.strip() for o in monthly_orders if o.customer)
    returning_count = sum(1 for c in current_customers if c in past_customers)
    new_count = len(current_customers) - returning_count
    customer_split_data = [new_count, returning_count]

    weekday_sales = {'Mon': 0, 'Tue': 0, 'Wed': 0, 'Thu': 0, 'Fri': 0, 'Sat': 0, 'Sun': 0}
    weekday_names = list(weekday_sales.keys())
    for order in monthly_orders:
        try:
            d = datetime.strptime(order.date, "%Y-%m-%d")
            weekday_sales[weekday_names[d.weekday()]] += order.total_price
        except ValueError:
            pass

    weekday_labels = list(weekday_sales.keys())
    weekday_data = list(weekday_sales.values())

    customer_stats = {}
    for order in monthly_orders:
        name = order.customer.strip()
        source = _normalize_source(order.source)
        if name not in customer_stats:
            customer_stats[name] = {'orders': 0, 'revenue': 0, 'sources': {}}
        customer_stats[name]['orders'] += 1
        customer_stats[name]['revenue'] += order.total_price
        customer_stats[name]['sources'][source] = customer_stats[name]['sources'].get(source, 0) + 1

    top_customers_all = []
    for name, stats in customer_stats.items():
        primary_source = max(stats['sources'], key=stats['sources'].get) if stats['sources'] else 'Other'
        top_customers_all.append({'name': name, 'orders': stats['orders'], 'revenue': stats['revenue'], 'source': primary_source})
    top_customers_all.sort(key=lambda x: x['revenue'], reverse=True)
    top_customers = top_customers_all[:10]

    forecast_revenue = 0
    if trend_data and view_mode == 'month':
        avg_recent = sum(trend_data[-7:]) / min(7, len(trend_data))
        forecast_revenue = int(avg_recent * num_periods)

    return {
        'period_label': period_label,
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'avg_ticket': int(avg_ticket),
        'unique_customers': unique_customers,
        'channels': channels,
        'top_items': top_items,
        'top_items_all': top_items_all,
        'estimated_profit': estimated_profit,
        'profit_margin': profit_margin,
        'items_per_order': items_per_order,
        'canceled_orders': canceled_orders,
        'refund_rate': refund_rate,
        'new_customers': new_count,
        'returning_customers': returning_count,
        'trend_labels': trend_labels,
        'trend_data': trend_data,
        'customer_split_data': customer_split_data,
        'weekday_labels': weekday_labels,
        'weekday_data': weekday_data,
        'top_customers': top_customers,
        'top_customers_all': top_customers_all,
        'forecast_revenue': forecast_revenue,
    }

@app.route('/monthly')
@manager_required
def monthly():
    db.session.remove()
    db.session.expire_all()

    view_mode = request.args.get('view', 'month')
    selected_month = request.args.get('month', get_myanmar_now().strftime('%Y-%m'))
    selected_year = request.args.get('year', get_myanmar_now().strftime('%Y'))

    report = _compute_monthly_report(view_mode, selected_month, selected_year)
    available_years = _available_years()

    return render_template(
        'monthly.html',
        active_page='monthly',
        view_mode=view_mode,
        selected_month=selected_month,
        selected_year=selected_year,
        available_years=available_years,
        **report
    )

@app.route('/monthly/export')
@manager_required
def monthly_export():
    view_mode = request.args.get('view', 'month')
    selected_month = request.args.get('month', get_myanmar_now().strftime('%Y-%m'))
    selected_year = request.args.get('year', get_myanmar_now().strftime('%Y'))

    report = _compute_monthly_report(view_mode, selected_month, selected_year)

    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(['Iris Flower — 业务报表 Business Report'])
    writer.writerow(['Period', report['period_label']])
    writer.writerow([])

    writer.writerow(['SUMMARY'])
    writer.writerow(['Gross Revenue (MMK)', report['total_revenue']])
    writer.writerow(['Completed Orders', report['total_orders']])
    writer.writerow(['Average Ticket (MMK)', report['avg_ticket']])
    writer.writerow(['Active Customers', report['unique_customers']])
    writer.writerow(['Estimated Gross Profit (MMK)', report['estimated_profit']])
    writer.writerow(['Profit Margin (%)', report['profit_margin']])
    writer.writerow(['Void Rate (%)', report['refund_rate']])
    writer.writerow(['Canceled/Zero-Value Orders', report['canceled_orders']])
    writer.writerow(['Items per Order', report['items_per_order']])
    writer.writerow(['New Customers', report['new_customers']])
    writer.writerow(['Returning Customers', report['returning_customers']])
    writer.writerow([])

    writer.writerow(['CHANNEL BREAKDOWN'])
    writer.writerow(['Channel', 'Revenue (MMK)', 'Percentage'])
    for c in report['channels']:
        writer.writerow([c['name'], c['revenue'], f"{c['percentage']}%"])
    writer.writerow([])

    writer.writerow(['TOP SELLING ITEMS'])
    writer.writerow(['Rank', 'Item Name', 'Units Sold', 'Revenue (MMK)'])
    for i, item in enumerate(report['top_items_all'], start=1):
        writer.writerow([i, item['name'], item['count'], item['revenue']])
    writer.writerow([])

    writer.writerow(['TOP CUSTOMERS'])
    writer.writerow(['Rank', 'Customer', 'Primary Source', 'Orders', 'Revenue (MMK)'])
    for i, c in enumerate(report['top_customers_all'], start=1):
        writer.writerow([i, c['name'], c['source'], c['orders'], c['revenue']])

    mem = io.BytesIO(('\ufeff' + buf.getvalue()).encode('utf-8'))
    label = report['period_label']
    filename = f"Iris_Flower_Report_{label}.csv"
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=filename)

@app.route('/run-migration')
@manager_required
def run_migration():
    ensure_schema()
    return "Schema check complete (new columns + wrappers table).<br><br><a href='/'>Back to app</a>"

@app.route('/toggle_payment/<int:order_id>', methods=['POST'])
@login_required
def toggle_payment(order_id):
    """Paid tick on an order card (staff + manager).
    Ticking requires who received the money and how (KPay / Cash);
    unticking clears all payment fields."""
    order = Order.query.get_or_404(order_id)
    data = request.get_json(silent=True) or {}
    is_paid = bool(data.get('is_paid'))

    if is_paid:
        who = (data.get('received_by') or '').strip()
        method = (data.get('payment_method') or '').strip()
        if who not in PAYMENT_RECEIVERS or method not in PAYMENT_METHODS:
            db.session.remove()
            return jsonify({'success': False, 'error': 'Choose who received it and the payment method'}), 400
        order.is_paid = True
        order.received_by = who
        order.payment_method = method
        order.payment_date = (data.get('payment_date') or '').strip() or get_myanmar_now().strftime('%Y-%m-%d')
    else:
        order.is_paid = False
        order.received_by = ''
        order.payment_method = ''
        order.payment_date = ''

    try:
        db.session.commit()
        return jsonify({
            'success': True,
            'is_paid': order.is_paid,
            'payment_date': order.payment_date,
            'received_by': order.received_by,
            'payment_method': order.payment_method,
        })
    except Exception as e:
        db.session.rollback()
        print("TOGGLE PAYMENT ERROR:", e)
        return jsonify({'success': False, 'error': 'Update failed'}), 500
    finally:
        db.session.remove()

@app.route('/admin/archive', methods=['GET'])
@manager_required
def archive_view():
    cutoff = request.args.get('before', (get_myanmar_now() - timedelta(days=365)).strftime('%Y-%m-%d'))
    count = Order.query.filter(Order.date < cutoff).count()
    return render_template('archive.html', active_page='archive', cutoff=cutoff, count=count)

@app.route('/admin/archive/export')
@manager_required
def archive_export():
    cutoff = request.args.get('before')
    if not cutoff:
        flash("Please choose a cutoff date.", 'error')
        return redirect(url_for('archive_view'))

    orders = (
        Order.query
        .options(joinedload(Order.items))
        .filter(Order.date < cutoff)
        .order_by(Order.date, Order.id)
        .all()
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        'order_id', 'date', 'source', 'customer', 'time', 'address', 'delivery_fee',
        'is_paid', 'payment_date', 'received_by', 'payment_method', 'order_total',
        'item_name', 'item_price', 'paper_color', 'wrapped_by', 'has_cake', 'photo_url', 'cake_photo_url', 'remarks'
    ])
    for o in orders:
        if not o.items:
            writer.writerow([o.id, o.date, o.source, o.customer, o.time, o.address, o.delivery_fee,
                              o.is_paid, o.payment_date, o.received_by, o.payment_method, o.total_price,
                              '', '', '', o.wrapped_by, '', '', '', ''])
        for item in o.items:
            writer.writerow([
                o.id, o.date, o.source, o.customer, o.time, o.address, o.delivery_fee,
                o.is_paid, o.payment_date, o.received_by, o.payment_method, o.total_price,
                item.item_name, item.price, item.paper_color, item.wrapped_by or o.wrapped_by,
                item.has_cake, item.photo_url, item.cake_photo_url, item.remarks
            ])

    mem = io.BytesIO(('\ufeff' + buf.getvalue()).encode('utf-8'))
    filename = f"Iris_Flower_Archive_before_{cutoff}.csv"
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=filename)

@app.route('/admin/archive/delete', methods=['POST'])
@manager_required
def archive_delete():
    cutoff = request.form.get('before')
    confirm_text = request.form.get('confirm_text', '')
    if confirm_text != 'DELETE':
        flash("You must type DELETE to confirm archival deletion.", 'error')
        return redirect(url_for('archive_view', before=cutoff))

    try:
        deleted = Order.query.filter(Order.date < cutoff).delete(synchronize_session=False)
        db.session.commit()
        flash(f"Archived and removed {deleted} orders from before {cutoff}.")
    except Exception as e:
        db.session.rollback()
        print("ARCHIVE DELETE ERROR:", e)
        flash("Archive deletion failed. No data was removed.", 'error')
    finally:
        db.session.remove()

    return redirect(url_for('archive_view'))

@app.route("/spreadsheet")
@manager_required
def spreadsheet():
    rows = []
    orders = Order.query.order_by(Order.date.desc()).all()
    for order in orders:
        for item in order.items:
            rows.append({"order": order, "item": item})
    return render_template("spreadsheet.html", rows=rows)

@app.route("/api/update-cell", methods=["POST"])
@manager_required
def update_cell():
    data = request.json

    table = data["table"]
    id = data["id"]
    field = data["field"]
    value = data["value"]

    # Whitelist editable fields so this endpoint can't touch ids/relations.
    allowed = {
        "order": {"date", "source", "customer", "time", "address", "wrapped_by", "is_paid", "delivery_fee",
                  "received_by", "payment_method"},
        "item": {"item_name", "price", "remarks", "paper_color", "wrapped_by"},
    }
    if table not in allowed or field not in allowed[table]:
        return {"success": False, "message": "Invalid field"}, 400

    obj = Order.query.get(id) if table == "order" else OrderItem.query.get(id)
    if not obj:
        return {"success": False, "message": "Not found"}, 404

    if field == "is_paid":
        value = True if value == "1" else False
    elif field in ("price", "delivery_fee"):
        value = _to_int(value)

    setattr(obj, field, value)
    db.session.commit()

    return {"success": True}

@app.route('/health')
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(debug=True, port=5000)
