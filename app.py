from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from sqlalchemy.orm import joinedload
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta, timezone
from functools import wraps
import os
from dotenv import load_dotenv
load_dotenv()
import cloudinary
import cloudinary.uploader
import cloudinary.api
import csv
import io

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
    MAX_CONTENT_LENGTH=16 * 1024 * 1024
)

# ==========================================
# CLOUDINARY CONFIGURATION
# ==========================================
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
    secure=True
)

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

def get_myanmar_now():
    """Returns a datetime object set explicitly to Myanmar Time (UTC +6:30)"""
    myanmar_tz = timezone(timedelta(hours=6, minutes=30))
    return datetime.now(myanmar_tz)
    
db = SQLAlchemy(app)

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
    delivery_fee = db.Column(db.Text, default='')
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    payment_date = db.Column(db.String(50), default='')

    items = db.relationship(
        'OrderItem',
        backref='order',
        cascade="all, delete-orphan",
        lazy='selectin'
    )

class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    item_name = db.Column(db.String(200), default='Cake')
    size = db.Column(db.String(50), default='-')
    price = db.Column(db.Integer, default=0)
    remarks = db.Column(db.Text, default='-')
    image_url = db.Column(db.Text, default='')
    flower_image_url = db.Column(db.Text, default='')

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

def _role_home_redirect():
    """Send the user back to whichever daily view matches their role."""
    if session.get('role') == 'staff':
        return redirect(url_for('staff_view'))
    return redirect(url_for('index'))

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
        elif username == "Memory Cake" and password == os.environ.get("MANAGER_PASSWORD", ""):
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
    """
    Daily Records filter, mirroring the Monthly Report filter bar: a
    day / month / year view-mode toggle plus one selected value.

    Returns (view_mode, filter_value) where view_mode is one of
    'day' | 'month' | 'year', and filter_value is the corresponding
    'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY' string.

    `default_view` controls what the page opens to when no ?view= is
    passed in the URL at all (e.g. the manager Daily page defaults to
    'month' so it doesn't just show "today" every time it's opened, while
    the staff Daily page keeps defaulting to 'day').
    """
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

def _safe_price(prices, i):
    """Index-safe price lookup. Staff forms omit price fields entirely
    (prices are manager-only), so `prices` may be shorter than the items
    list, or empty. Missing/blank/invalid values default to 0."""
    try:
        if i < len(prices) and prices[i] not in (None, ''):
            return int(prices[i])
    except (ValueError, TypeError):
        pass
    return 0

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
    """Shared grouping logic: run an already-filtered Order query and bucket
    the results by date, newest first. Used by both the on-page dashboard
    (day / month / year) and the /api/export_orders endpoint (day / month /
    year), so the two can never disagree on shape."""
    orders = query.order_by(Order.date.desc(), Order.id.desc()).all()

    recent_cutoff = (get_myanmar_now() - timedelta(days=30)).strftime('%Y-%m-%d')

    groups = {}
    for order in orders:
        order.is_recent = order.date >= recent_cutoff  # controls Cloudinary URL visibility on-page
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

def _fetch_orders_for_period(mode, value, source=None):
    """Fetches orders scoped to a single day, a whole month, or a whole
    year, grouped by day. Shared by the on-page Daily Records dashboard
    and the /api/export_orders PDF-export feed, so the two can never drift
    out of sync.

    `source`, when given, restricts results to orders whose source exactly
    matches it (manager-only "Filter by Source" control on the Daily
    Records page)."""
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

    return _group_query_by_day(query)

# Kept as a thin alias: some code/comments still refer to the "grouped by
# day" helper by its original name.
_fetch_orders_grouped_by_day = _fetch_orders_for_period
_fetch_orders_for_export = _fetch_orders_for_period

def _available_years():
    years = sorted({
        r[0][:4]
        for r in Order.query.with_entities(Order.date).distinct().all()
        if r[0] and len(r[0]) >= 4
    }, reverse=True)
    return years or [get_myanmar_now().strftime('%Y')]

def _daily_view_context(filter_action, default_view='day'):
    view_mode, filter_value, selected_day, selected_month, selected_year = _parse_daily_filters(default_view)
    # Manager-only "Filter by Source" control on the Daily Records filter
    # bar. Staff never send this param (they have no UI for it), so it's
    # always empty for the staff view — kept in this shared helper anyway
    # so the two views can never drift apart on filtering logic.
    selected_source = (request.args.get('source') or '').strip()
    orders_by_day = _fetch_orders_for_period(view_mode, filter_value, source=selected_source or None)
    total_orders = sum(d['order_count'] for d in orders_by_day)
    total_revenue = sum(d['day_total'] for d in orders_by_day)

    return {
        'orders_by_day': orders_by_day,
        'view_mode': view_mode,
        'selected_day': selected_day,
        'selected_month': selected_month,
        'selected_year': selected_year,
        'selected_source': selected_source,
        'available_years': _available_years(),
        'filter_action': filter_action,
        'total_orders': total_orders,
        'total_revenue': total_revenue,
    }

def _serialize_order_for_export(o):
    return {
        'id': o.id,
        'date': o.date,
        'customer': o.customer,
        'source': o.source,
        'time': o.time,
        'address': o.address,
        'delivery_fee': o.delivery_fee or '',
        'total_price': o.total_price,
        'is_paid': bool(o.is_paid),
        'payment_date': o.payment_date or '',
        'items': [
            {
                'item_name': it.item_name,
                'size': it.size,
                'price': it.price,
                'remarks': it.remarks,
                'image_url': it.image_url or '',
                'flower_image_url': it.flower_image_url or '',
            }
            for it in o.items
        ],
    }

def _extract_cloudinary_public_id(url):
    """Given a Cloudinary secure_url, return its public_id (including folder), or None."""
    if not url or 'cloudinary.com' not in url or '/upload/' not in url:
        return None
    try:
        after_upload = url.split('/upload/', 1)[1]
        parts = after_upload.split('/')
        # Drop the version segment, e.g. 'v1728490213'
        if parts and parts[0].startswith('v') and parts[0][1:].isdigit():
            parts = parts[1:]
        path_with_ext = '/'.join(parts)
        public_id = path_with_ext.rsplit('.', 1)[0]  # strip file extension
        return public_id or None
    except Exception:
        return None

def _delete_cloudinary_asset(url):
    """Best-effort delete of a Cloudinary image given its stored URL. Never raises."""
    public_id = _extract_cloudinary_public_id(url)
    if not public_id:
        return
    try:
        result = cloudinary.uploader.destroy(public_id, resource_type="image")
        print(f"[Cloudinary] destroy {public_id}: {result.get('result')}")
    except Exception as e:
        print(f"[Cloudinary DELETE ERROR] {public_id}: {e}")
# ==========================================
# MAIN ROUTES
# ==========================================
@app.route('/')
@manager_required
def index():
    db.session.remove()
    # Manager Daily view opens to "this month" by default (instead of
    # "today") so it isn't just showing a near-empty single day every time
    # the page loads. Explicitly passing ?view=day/year still works as usual.
    ctx = _daily_view_context(url_for('index'), default_view='month')

    # The PDF export button re-uses whichever filter (day/month/year, plus
    # source if one is active) is currently active on the page, instead of
    # opening its own picker.
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

    return render_template(
        'daily.html', active_page='daily', readonly=False, show_payment=True,
        export_params=export_params, period_value=period_value, **ctx
    )

@app.route('/staff')
@login_required
def staff_view():
    if session.get('role') == 'manager':
        return redirect(url_for('index'))
    db.session.remove()
    ctx = _daily_view_context(url_for('staff_view'))
    return render_template('staff_daily.html', active_page='staff', readonly=True, show_payment=False, **ctx)

@app.route('/api/export_orders')
@manager_required
def api_export_orders():
    """JSON data feed for the client-side PDF export on the Daily page.
    Returns orders grouped by day, same shape the on-page dashboard uses,
    scoped to a single day, a whole month, or a whole year, and optionally
    to a single source (mirrors the manager-only Filter by Source control).

    Kept manager-only and JSON-only (no page render, no image-hosting
    special-casing) so it stays fast even for a full year of orders."""
    db.session.remove()

    day = (request.args.get('day') or '').strip()
    month = (request.args.get('month') or '').strip()
    year = (request.args.get('year') or '').strip()
    view = (request.args.get('view') or '').strip()
    source = (request.args.get('source') or '').strip()

    if day:
        mode, value = 'day', day
    elif month:
        mode, value = 'month', month
    elif year or view == 'year':
        mode, value = 'year', (year or get_myanmar_now().strftime('%Y'))
    else:
        mode, value = 'all', ''

    groups = _fetch_orders_for_period(mode, value, source=source or None)
    total_orders = sum(g['order_count'] for g in groups)
    total_revenue = sum(g['day_total'] for g in groups)

    payload = {
        'mode': mode,
        'value': value,
        'source': source,
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
    # Only managers are allowed to mark an order as paid. Staff never send
    # this field at all (it's removed from their form), but we also guard
    # server-side in case of a crafted request.
    if _is_manager():
        is_paid = request.form.get('is_paid') == 'on'
        payment_date = request.form.get('payment_date') or ''
    else:
        is_paid = False
        payment_date = ''

    order_date = request.form.get('date')
    source = request.form.get('source') or '-'
    customer = request.form.get('customer')
    time = request.form.get('time') or '-'
    address = request.form.get('address') or '-'
    delivery_fee = request.form.get('delivery_fee') or ''

    item_names = request.form.getlist('item_name[]')
    sizes = request.form.getlist('size[]')
    prices = request.form.getlist('item_price[]')
    remarks_list = request.form.getlist('remarks[]')

    uploaded_flowers = request.files.getlist('flowerImage[]')
    uploaded_cakes = request.files.getlist('cakeImage[]')

    new_order = Order(
        date=order_date, source=source, customer=customer, total_price=0, time=time, address=address,
        delivery_fee=delivery_fee, is_paid=is_paid, payment_date=payment_date
    )
    db.session.add(new_order)
    db.session.flush()

    calculated_total = 0
    timestamp_prefix = int(datetime.now().timestamp())

    for i in range(len(item_names)):
        item_price = _safe_price(prices, i)
        calculated_total += item_price

        flower_url = ""
        if i < len(uploaded_flowers):
            f_file = uploaded_flowers[i]
            if f_file and f_file.filename != '':
                try:
                    f_file.stream.seek(0)
                    result = cloudinary.uploader.upload(
                        f_file.stream,
                        folder="memory_cake/flowers",
                        public_id=f"{timestamp_prefix}_flr_{i}",
                        overwrite=True,
                        resource_type="image"
                    )
                    flower_url = result['secure_url']
                    print(f"[Cloudinary] flower uploaded OK: {flower_url}")
                except Exception as e:
                    err = f"图片上传失败 flower item {i+1}: {e}"
                    print(f"[Cloudinary ERROR] {err}")
                    flash(err, 'error')

        cake_url = ""
        if i < len(uploaded_cakes):
            c_file = uploaded_cakes[i]
            if c_file and c_file.filename != '':
                try:
                    c_file.stream.seek(0)
                    result = cloudinary.uploader.upload(
                        c_file.stream,
                        folder="memory_cake/cakes",
                        public_id=f"{timestamp_prefix}_cke_{i}",
                        overwrite=True,
                        resource_type="image"
                    )
                    cake_url = result['secure_url']
                    print(f"[Cloudinary] cake uploaded OK: {cake_url}")
                except Exception as e:
                    err = f"图片上传失败 cake item {i+1}: {e}"
                    print(f"[Cloudinary ERROR] {err}")
                    flash(err, 'error')

        sub_item = OrderItem(
            order_id=new_order.id,
            item_name=item_names[i] or 'Cake',
            size=sizes[i] or '-',
            price=item_price,
            remarks=remarks_list[i] or '-',
            image_url=cake_url,
            flower_image_url=flower_url
        )
        db.session.add(sub_item)

    new_order.total_price = calculated_total
    db.session.commit()
    db.session.remove()
    if session.get('role') == 'staff':
        return redirect(url_for('staff_view'))
    return redirect(url_for('index'))

@app.route('/delete_order/<int:id>', methods=['GET', 'POST'])
@login_required
def delete_order(id):
    order = Order.query.options(joinedload(Order.items)).get_or_404(id)
    try:
        for item in order.items:
            if item.image_url:
                _delete_cloudinary_asset(item.image_url)
            if item.flower_image_url:
                _delete_cloudinary_asset(item.flower_image_url)

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
    order.date = request.form.get('date')
    order.source = request.form.get('source') or '-'
    order.customer = request.form.get('customer')
    order.time = request.form.get('time') or '-'
    order.address = request.form.get('address') or '-'
    order.delivery_fee = request.form.get('delivery_fee') or ''

    # Payment status (is_paid / payment_date) is intentionally NOT touched
    # here for either role. It used to be a checkbox buried inside this
    # edit form, but it's now managed directly from the order card via the
    # "Mark as Paid" toggle (see /toggle_payment below), so the edit form
    # no longer needs to carry or submit that field at all.

    names = request.form.getlist('edit_item_name[]')
    sizes = request.form.getlist('edit_size[]')
    prices = request.form.getlist('edit_price[]')
    remarks = request.form.getlist('edit_remarks[]')

    old_cake_images = request.form.getlist('old_cake_image[]')
    old_flower_images = request.form.getlist('old_flower_image[]')
    new_cake_files = request.files.getlist('editCakeImage[]')
    new_flower_files = request.files.getlist('editFlowerImage[]')
    timestamp_prefix = int(datetime.now().timestamp())

    # Snapshot every image URL this order currently owns, BEFORE any DB changes.
    existing_items = OrderItem.query.filter_by(order_id=order.id).all()
    urls_before = set()
    for oi in existing_items:
        if oi.image_url:
            urls_before.add(oi.image_url)
        if oi.flower_image_url:
            urls_before.add(oi.flower_image_url)

    urls_after = set()

    try:
        OrderItem.query.filter_by(order_id=order.id).delete()
        total_price = 0

        for i in range(len(names)):
            if not names[i].strip():
                continue
            price = _safe_price(prices, i)
            total_price += price

            cake_url = old_cake_images[i] if i < len(old_cake_images) else ''
            if i < len(new_cake_files):
                c_file = new_cake_files[i]
                if c_file and c_file.filename != '':
                    try:
                        c_file.stream.seek(0)
                        result = cloudinary.uploader.upload(
                            c_file.stream,
                            folder="memory_cake/cakes",
                            public_id=f"{timestamp_prefix}_editcke_{i}",
                            overwrite=True,
                            resource_type="image"
                        )
                        cake_url = result['secure_url']
                        print(f"[Cloudinary] edit cake uploaded OK: {cake_url}")
                    except Exception as e:
                        err = f"图片上传失败 edit cake item {i+1}: {e}"
                        print(f"[Cloudinary ERROR] {err}")
                        flash(err, 'error')

            flower_url = old_flower_images[i] if i < len(old_flower_images) else ''
            if i < len(new_flower_files):
                f_file = new_flower_files[i]
                if f_file and f_file.filename != '':
                    try:
                        f_file.stream.seek(0)
                        result = cloudinary.uploader.upload(
                            f_file.stream,
                            folder="memory_cake/flowers",
                            public_id=f"{timestamp_prefix}_editflr_{i}",
                            overwrite=True,
                            resource_type="image"
                        )
                        flower_url = result['secure_url']
                        print(f"[Cloudinary] edit flower uploaded OK: {flower_url}")
                    except Exception as e:
                        err = f"图片上传失败 edit flower item {i+1}: {e}"
                        print(f"[Cloudinary ERROR] {err}")
                        flash(err, 'error')

            if cake_url:
                urls_after.add(cake_url)
            if flower_url:
                urls_after.add(flower_url)

            new_item = OrderItem(
                order_id=order.id,
                item_name=names[i],
                size=sizes[i] if i < len(sizes) else '-',
                price=price,
                remarks=remarks[i] if i < len(remarks) else '-',
                image_url=cake_url,
                flower_image_url=flower_url
            )
            db.session.add(new_item)

        order.total_price = total_price
        db.session.commit()

        # Anything that existed before but isn't referenced anymore = orphaned. Clean it up.
        orphaned_urls = urls_before - urls_after
        for url in orphaned_urls:
            _delete_cloudinary_asset(url)

        flash('Order updated successfully.')
    except Exception as e:
        db.session.rollback()
        print("EDIT ERROR:", e)
        flash("Failed to update order. Please try again.")
    finally:
        db.session.remove()

    return _role_home_redirect()

# ==========================================
# REPORTING
# ==========================================
def _normalize_source(source):
    source = (source or '').strip()
    if source in ['', '-']:
        return 'Other'
    return source

def _compute_monthly_report(view_mode, selected_month, selected_year):
    """Shared aggregation logic for both the /monthly dashboard page and the
    /monthly/export CSV download, so the two can never drift out of sync."""
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
            size = item.size or '-'
            price = item.price or 0
            if item_name not in item_stats:
                item_stats[item_name] = {'count': 0, 'revenue': 0, 'sizes': set()}
            item_stats[item_name]['count'] += 1
            item_stats[item_name]['revenue'] += price
            if size != '-':
                item_stats[item_name]['sizes'].add(size)

    top_items = []
    for name, stats in item_stats.items():
        top_items.append({
            'name': name,
            'sizes': ' / '.join(stats['sizes']) if stats['sizes'] else 'Standard',
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
    """CSV export of the monthly/annual report (summary + channels + top
    items + top customers) for whichever period is currently selected."""
    view_mode = request.args.get('view', 'month')
    selected_month = request.args.get('month', get_myanmar_now().strftime('%Y-%m'))
    selected_year = request.args.get('year', get_myanmar_now().strftime('%Y'))

    report = _compute_monthly_report(view_mode, selected_month, selected_year)

    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(['Memory Cake — 业务报表 Business Report'])
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
    writer.writerow(['Rank', 'Item Name', 'Common Sizes', 'Units Sold', 'Revenue (MMK)'])
    for i, item in enumerate(report['top_items_all'], start=1):
        writer.writerow([i, item['name'], item['sizes'], item['count'], item['revenue']])
    writer.writerow([])

    writer.writerow(['TOP CUSTOMERS'])
    writer.writerow(['Rank', 'Customer', 'Primary Source', 'Orders', 'Revenue (MMK)'])
    for i, c in enumerate(report['top_customers_all'], start=1):
        writer.writerow([i, c['name'], c['source'], c['orders'], c['revenue']])

    mem = io.BytesIO(('\ufeff' + buf.getvalue()).encode('utf-8'))
    label = report['period_label']
    filename = f"Memory_Cake_Report_{label}.csv"
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=filename)


@app.route('/check-cloudinary')
@manager_required
def check_cloudinary():
    """Diagnostic: verify Cloudinary credentials are loaded and working."""
    cfg = cloudinary.config()
    cloud = cfg.cloud_name or 'NOT SET'
    key = cfg.api_key or 'NOT SET'
    secret = '✅ set' if cfg.api_secret else '❌ NOT SET'
    try:
        result = cloudinary.api.ping()
        ping = f"✅ {result}"
    except Exception as e:
        ping = f"❌ {e}"
    return (
        f"<b>cloud_name:</b> {cloud}<br>"
        f"<b>api_key:</b> {key}<br>"
        f"<b>api_secret:</b> {secret}<br>"
        f"<b>ping:</b> {ping}<br><br>"
        f"<a href='/'>← Back to app</a>"
    )


@app.route('/debug-upload')
@manager_required
def debug_upload():
    """Test Cloudinary config and do a real tiny upload to confirm it works end-to-end."""
    import io
    lines = []
    cfg = cloudinary.config()
    lines.append(f"<b>cloud_name:</b> {cfg.cloud_name or '❌ NOT SET'}")
    lines.append(f"<b>api_key:</b> {cfg.api_key or '❌ NOT SET'}")
    lines.append(f"<b>api_secret:</b> {'✅ set' if cfg.api_secret else '❌ NOT SET'}")

    # Ping
    try:
        ping = cloudinary.api.ping()
        lines.append(f"<b>ping:</b> ✅ {ping.get('status')}")
    except Exception as e:
        lines.append(f"<b>ping:</b> ❌ {e}")
        return "<br>".join(lines) + "<br><br><a href='/'>← Back</a>"

    # Attempt a real upload of a 1×1 white PNG
    try:
        tiny_png = (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00'
            b'\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18'
            b'\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        )
        result = cloudinary.uploader.upload(
            io.BytesIO(tiny_png),
            folder="memory_cake/test",
            public_id="debug_ping_test",
            overwrite=True,
            resource_type="image"
        )
        lines.append(f"<b>test upload:</b> ✅ <a href='{result['secure_url']}' target='_blank'>{result['secure_url']}</a>")
    except Exception as e:
        lines.append(f"<b>test upload:</b> ❌ {e}")

    return "<br>".join(lines) + "<br><br><a href='/'>← Back</a>"


@app.route('/test-upload-form', methods=['GET', 'POST'])
@manager_required
def test_upload_form():
    """Simple isolated upload test to diagnose form file delivery.

    Use ?mode=raw to inspect the unparsed WSGI body (proves whether bytes
    arrive at all at the gunicorn/WSGI layer).
    Use ?mode=files (default) to run the normal Werkzeug file-parsing path.
    These two modes are mutually exclusive in a single request: reading the
    raw body first will exhaust the stream and request.files will then
    always report empty, which would falsely look like the bug itself.
    """
    mode = request.args.get('mode', 'files')

    if request.method == 'POST':
        lines = []

        # Headers are always safe to read; they don't touch the body stream.
        environ = request.environ
        lines.append("<b>Mode:</b> " + mode)
        lines.append("<b>CONTENT_LENGTH header:</b> " + str(environ.get('CONTENT_LENGTH', '<not set>')))
        lines.append("<b>CONTENT_TYPE header:</b> " + str(environ.get('CONTENT_TYPE', '<not set>')))
        lines.append("<b>Transfer-Encoding header:</b> " + str(environ.get('HTTP_TRANSFER_ENCODING', '<not set>')))
        lines.append("<b>wsgi.input type:</b> " + str(type(environ.get('wsgi.input'))))

        if mode == 'raw':
            # This branch deliberately does NOT touch request.files/request.form.
            try:
                raw_body = request.get_data(cache=True, parse_form_data=False)
                lines.append("<b>Raw body length via get_data():</b> " + str(len(raw_body)))
                if len(raw_body) > 0:
                    lines.append("<b>Raw body first 300 bytes (repr):</b> " + repr(raw_body[:300]))
                    lines.append("<b>Raw body last 100 bytes (repr):</b> " + repr(raw_body[-100:]))
                else:
                    lines.append("<b>Raw body is EMPTY at the WSGI layer — bytes never reached gunicorn/Flask. Look at the reverse proxy / client, not the Python code.</b>")
            except Exception as e:
                lines.append("<b>get_data() raised:</b> " + repr(e))

        else:
            # Normal high-level Werkzeug form/file parsing path (this is what
            # the rest of the app actually uses for real uploads).
            file_keys = list(request.files.keys())
            form_keys = list(request.form.keys())
            lines.append("<b>Files in request:</b> " + str(file_keys))
            lines.append("<b>Form fields:</b> " + str(form_keys))

            uploaded = request.files.get('testfile')
            if not uploaded:
                lines.append("<b>No file received — form not sending files</b>")
            elif uploaded.filename == '':
                lines.append("<b>File received but filename is empty</b>")
            else:
                lines.append("<b>File received:</b> " + uploaded.filename + " (" + uploaded.content_type + ")")
                lines.append("<b>uploaded.content_length attr:</b> " + str(getattr(uploaded, 'content_length', '<n/a>')))
                try:
                    pos_before = uploaded.stream.tell()
                    lines.append("<b>stream position before read:</b> " + str(pos_before))
                except Exception as e:
                    lines.append("<b>stream.tell() failed:</b> " + repr(e))
                uploaded.stream.seek(0)
                file_bytes = uploaded.stream.read()
                lines.append("<b>Bytes read:</b> " + str(len(file_bytes)))
                if len(file_bytes) > 0:
                    try:
                        result = cloudinary.uploader.upload(
                            file_bytes,
                            folder="memory_cake/test",
                            public_id="test_real_upload",
                            overwrite=True,
                            resource_type="image"
                        )
                        url = result['secure_url']
                        lines.append("<b>Cloudinary upload OK:</b> <a href='" + url + "' target='_blank'>View image</a>")
                    except Exception as e:
                        lines.append("<b>Cloudinary upload failed:</b> " + str(e))
                else:
                    lines.append("<b>file_bytes is empty after read()</b>")

        return "<br><br>".join(lines) + "<br><br><a href='/test-upload-form'>Try again (files mode)</a> | <a href='/test-upload-form?mode=raw'>Try again (raw mode)</a> | <a href='/'>Back</a>"

    return """
    <html><body style="font-family:sans-serif;padding:40px">
    <h2>Upload Test</h2>
    <p>Mode: """ + mode + """ — <a href='?mode=files'>files mode</a> | <a href='?mode=raw'>raw mode</a></p>
    <form method="POST" enctype="multipart/form-data" action="?mode=""" + mode + """">
        <input type="file" name="testfile" accept="image/*"><br><br>
        <button type="submit">Upload to Cloudinary</button>
    </form>
    </body></html>
    """


@app.route('/run-migration')
@manager_required
def run_migration():
    results = []
    with db.engine.connect() as conn:
        from sqlalchemy import text
        for col, col_type, default in [
            ('image_url', 'TEXT', "''"),
            ('flower_image_url', 'TEXT', "''"),
            ('is_paid', 'BOOLEAN', 'FALSE'),
            ('payment_date', 'TEXT', "''"),
            ('delivery_fee', 'TEXT', "''"),
        ]:
            try:
                table = 'order_items' if col in ('image_url', 'flower_image_url') else 'orders'
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type} DEFAULT {default}"))
                conn.commit()
                results.append(f"Added column: {table}.{col}")
            except Exception as e:
                results.append(f"{col}: {str(e).split('ERROR:')[-1].strip()}")
    return "<br>".join(results) + "<br><br><a href='/'>Back to app</a>"

@app.route('/toggle_payment/<int:order_id>', methods=['POST'])
@manager_required
def toggle_payment(order_id):
    order = Order.query.get_or_404(order_id)
    data = request.get_json(silent=True) or {}
    is_paid = bool(data.get('is_paid'))
    custom_date = (data.get('payment_date') or '').strip()

    order.is_paid = is_paid
    order.payment_date = (custom_date or get_myanmar_now().strftime('%Y-%m-%d')) if is_paid else ''

    try:
        db.session.commit()
        return jsonify({'success': True, 'is_paid': order.is_paid, 'payment_date': order.payment_date})
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
        'order_id', 'date', 'source', 'customer', 'time', 'address',
        'is_paid', 'payment_date', 'order_total',
        'item_name', 'size', 'item_price', 'remarks', 'image_url', 'flower_image_url'
    ])
    for o in orders:
        if not o.items:
            writer.writerow([o.id, o.date, o.source, o.customer, o.time, o.address,
                              o.is_paid, o.payment_date, o.total_price, '', '', '', '', '', ''])
        for item in o.items:
            writer.writerow([
                o.id, o.date, o.source, o.customer, o.time, o.address,
                o.is_paid, o.payment_date, o.total_price,
                item.item_name, item.size, item.price, item.remarks,
                item.image_url, item.flower_image_url
            ])

    mem = io.BytesIO(('\ufeff' + buf.getvalue()).encode('utf-8'))
    filename = f"Memory_Cake_Archive_before_{cutoff}.csv"
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
    orders = Order.query.order_by(
        Order.date.desc()
    ).all()
    for order in orders:
        for item in order.items:
            rows.append({
                "order": order,
                "item": item
            })
    return render_template(
        "spreadsheet.html",
        rows=rows
    )

@app.route("/api/update-cell", methods=["POST"])
def update_cell():

    data = request.json

    table = data["table"]
    id = data["id"]
    field = data["field"]
    value = data["value"]

    if table == "order":
        obj = Order.query.get(id)

    elif table == "item":
        obj = OrderItem.query.get(id)

    else:
        return {
            "success": False,
            "message": "Invalid table"
        }

    if not obj:
        return {
            "success": False,
            "message": "Not found"
        }

    if field == "is_paid":
        value = True if value == "1" else False

    setattr(obj, field, value)

    db.session.commit()

    return {
        "success": True
    }
    
@app.route('/health')
def health():
    return "OK", 200

if __name__ == '__main__':
    app.run(debug=True, port=5000)
