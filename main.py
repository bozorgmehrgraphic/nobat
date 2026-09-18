import os
import json
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, abort)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
import jdatetime

# ---------- App setup ----------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

db_url = os.environ.get('DATABASE_URL', 'sqlite:///data.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'لطفاً وارد شوید.'

# ثابت‌ها
QUEUE_EXPIRE_DAYS = 7
LOADED_EXPIRE_DAYS = 7
WAIT_GREEN_DAYS = 2
WAIT_YELLOW_DAYS = 4

# ---------- Models ----------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Destination(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)


class KnownPlate(db.Model):
    plate_key = db.Column(db.String(20), primary_key=True)
    name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    vet_code = db.Column(db.String(20))
    tonnage = db.Column(db.String(20))
    destinations_json = db.Column(db.Text, default='[]')
    last_used = db.Column(db.DateTime, default=datetime.utcnow)

    def get_destinations(self):
        try:
            return json.loads(self.destinations_json or '[]')
        except Exception:
            return []


class Driver(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    queue_code = db.Column(db.String(10))
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    vet_code = db.Column(db.String(20), nullable=False)
    tonnage = db.Column(db.String(20), nullable=False)
    plate_two = db.Column(db.String(5), default='')
    plate_three = db.Column(db.String(5), default='')
    plate_city = db.Column(db.String(5), default='')
    destinations_json = db.Column(db.Text, default='[]')
    status = db.Column(db.String(20), default='waiting')  # waiting | loaded
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    timer_start_at = db.Column(db.DateTime, default=datetime.utcnow)
    loaded_at = db.Column(db.DateTime)
    cargo_dest = db.Column(db.String(100))
    cargo_name = db.Column(db.String(200))
    cargo_ton = db.Column(db.String(20))

    user = db.relationship('User', backref='driver_entries')

    def get_destinations(self):
        try:
            return json.loads(self.destinations_json or '[]')
        except Exception:
            return []

    @property
    def plate_key(self):
        return f"{self.plate_two}-{self.plate_three}-{self.plate_city}"


# ---------- Helpers ----------
PERSIAN_DIGITS = '۰۱۲۳۴۵۶۷۸۹'
EN_DIGITS = '0123456789'
_TR_FA = str.maketrans(EN_DIGITS, PERSIAN_DIGITS)
_TR_EN = str.maketrans(PERSIAN_DIGITS + '٠١٢٣٤٥٦٧٨٩',
                       EN_DIGITS + EN_DIGITS)


def to_fa(s):
    if s is None:
        return ''
    return str(s).translate(_TR_FA)


def fa_to_en(s):
    if s is None:
        return ''
    return str(s).translate(_TR_EN)


def wait_seconds(driver):
    start = driver.created_at or datetime.utcnow()
    return (datetime.utcnow() - start).total_seconds()


def timer_seconds(driver):
    start = driver.timer_start_at or driver.created_at or datetime.utcnow()
    return (datetime.utcnow() - start).total_seconds()


def wait_days(driver):
    return wait_seconds(driver) / 86400


def waiting_class(driver):
    d = wait_days(driver)
    if d <= WAIT_GREEN_DAYS:
        return 'fresh'
    if d <= WAIT_YELLOW_DAYS:
        return 'mid'
    return 'late'


def wait_text(driver):
    s = wait_seconds(driver)
    if s < 60:
        return 'همین الان'
    m = int(s // 60)
    if m < 60:
        return f'{to_fa(m)} دقیقه'
    h = m // 60
    if h < 24:
        rem = m % 60
        return f'{to_fa(h)} ساعت' + (f' و {to_fa(rem)} دقیقه' if rem else '')
    days = h // 24
    rem_h = h % 24
    return f'{to_fa(days)} روز' + (f' و {to_fa(rem_h)} ساعت' if rem_h else '')


def expiry_text(driver):
    remaining = QUEUE_EXPIRE_DAYS * 86400 - timer_seconds(driver)
    if remaining <= 0:
        return 'در حال حذف'
    m = int(remaining // 60)
    if m < 60:
        return f'{to_fa(m)} دقیقه مانده'
    h = m // 60
    if h < 24:
        return f'{to_fa(h)} ساعت مانده'
    days = h // 24
    rem_h = h % 24
    return f'{to_fa(days)} روز' + (f' و {to_fa(rem_h)} ساعت' if rem_h else '') + ' مانده'


def is_expiring_soon(driver):
    return (QUEUE_EXPIRE_DAYS * 86400 - timer_seconds(driver)) < (2 * 86400)


@app.template_filter('fa')
def fa_filter(s):
    return to_fa(s)


@app.template_filter('fadate')
def fa_date_filter(dt):
    if not dt:
        return ''
    try:
        j = jdatetime.datetime.fromgregorian(datetime=dt)
        return j.strftime('%Y/%m/%d - %H:%M')
    except Exception:
        return dt.strftime('%Y/%m/%d - %H:%M')


@app.context_processor
def inject_helpers():
    return dict(
        wait_text=wait_text,
        expiry_text=expiry_text,
        waiting_class=waiting_class,
        is_expiring_soon=is_expiring_soon,
    )


def clean_expired():
    now = datetime.utcnow()
    q_limit = now - timedelta(days=QUEUE_EXPIRE_DAYS)
    l_limit = now - timedelta(days=LOADED_EXPIRE_DAYS)
    removed = 0
    for d in Driver.query.filter_by(status='waiting').all():
        start = d.timer_start_at or d.created_at
        if start and start < q_limit:
            db.session.delete(d)
            removed += 1
    for d in Driver.query.filter_by(status='loaded').all():
        if d.loaded_at and d.loaded_at < l_limit:
            db.session.delete(d)
            removed += 1
    if removed:
        db.session.commit()
    return removed


_last_cleanup = {'t': None}


def maybe_cleanup():
    now = datetime.utcnow()
    if _last_cleanup['t'] is None or (now - _last_cleanup['t']).total_seconds() > 1800:
        try:
            clean_expired()
        except Exception as e:
            app.logger.warning(f'cleanup error: {e}')
        _last_cleanup['t'] = now


def next_queue_code():
    mx = 0
    for d in Driver.query.all():
        if d.queue_code:
            try:
                n = int(fa_to_en(d.queue_code))
                if n > mx:
                    mx = n
            except Exception:
                pass
    return str(mx + 1).zfill(4)


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*a, **kw)
    return wrapper


# ---------- Routes: auth ----------
@app.route('/')
def index():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    if current_user.is_admin:
        return redirect(url_for('manager'))
    return redirect(url_for('driver_dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        flash('نام کاربری یا رمز عبور اشتباه است.', 'error')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        name = request.form.get('name', '').strip()
        phone = fa_to_en(request.form.get('phone', '').strip())
        if not (username and password and name and phone):
            flash('همه فیلدها را پر کنید.', 'error')
        elif password != password2:
            flash('رمز عبور و تکرار آن یکسان نیستند.', 'error')
        elif len(password) < 4:
            flash('رمز عبور حداقل ۴ کاراکتر باشد.', 'error')
        elif User.query.filter_by(username=username).first():
            flash('این نام کاربری قبلاً استفاده شده است.', 'error')
        else:
            user = User(username=username, name=name, phone=phone, is_admin=False)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash('ثبت‌نام با موفقیت انجام شد.', 'ok')
            return redirect(url_for('driver_dashboard'))
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ---------- Routes: driver ----------
@app.route('/driver')
@login_required
def driver_dashboard():
    if current_user.is_admin:
        return redirect(url_for('manager'))
    entry = (Driver.query
             .filter_by(user_id=current_user.id)
             .filter(Driver.status.in_(['waiting', 'loaded']))
             .order_by(Driver.id.desc()).first())
    position = None
    if entry and entry.status == 'waiting':
        waiters = Driver.query.filter_by(status='waiting').order_by(Driver.created_at).all()
        for i, d in enumerate(waiters, 1):
            if d.id == entry.id:
                position = i
                break
    all_dests = Destination.query.order_by(Destination.name).all()
    return render_template('driver_dashboard.html',
                           entry=entry, position=position,
                           all_dests=all_dests)


@app.route('/driver/queue', methods=['POST'])
@login_required
def driver_queue():
    if current_user.is_admin:
        abort(403)
    existing = Driver.query.filter_by(user_id=current_user.id, status='waiting').first()
    if existing:
        flash('شما هم‌اکنون در صف هستید.', 'error')
        return redirect(url_for('driver_dashboard'))

    name = request.form.get('name', '').strip() or current_user.name
    phone = fa_to_en(request.form.get('phone', '').strip()) or current_user.phone
    vet = fa_to_en(request.form.get('vet_code', '').strip())
    tonnage = fa_to_en(request.form.get('tonnage', '').strip())
    p_two = request.form.get('plate_two', '').strip()
    p_three = request.form.get('plate_three', '').strip()
    p_city = request.form.get('plate_city', '').strip()
    destinations = request.form.getlist('destinations')[:3]

    if not (name and phone and vet and tonnage and p_two and p_three and p_city):
        flash('همه فیلدها را کامل وارد کنید.', 'error')
        return redirect(url_for('driver_dashboard'))
    if not vet.isdigit():
        flash('کد دامپزشکی باید فقط عدد باشد.', 'error')
        return redirect(url_for('driver_dashboard'))
    try:
        if float(tonnage) <= 0:
            raise ValueError
    except Exception:
        flash('تناژ باید عدد مثبت باشد.', 'error')
        return redirect(url_for('driver_dashboard'))
    if not destinations:
        flash('حداقل یک مقصد انتخاب کنید.', 'error')
        return redirect(url_for('driver_dashboard'))

    pk = f'{p_two}-{p_three}-{p_city}'
    for d in Driver.query.filter_by(status='waiting').all():
        if d.plate_key == pk:
            flash('این پلاک هم‌اکنون در صف است.', 'error')
            return redirect(url_for('driver_dashboard'))

    qcode = next_queue_code()
    now = datetime.utcnow()
    entry = Driver(
        user_id=current_user.id, queue_code=qcode,
        name=name, phone=phone, vet_code=vet, tonnage=tonnage,
        plate_two=p_two, plate_three=p_three, plate_city=p_city,
        destinations_json=json.dumps(destinations),
        status='waiting', created_at=now, timer_start_at=now,
    )
    db.session.add(entry)

    kp = db.session.get(KnownPlate, pk)
    if not kp:
        kp = KnownPlate(plate_key=pk)
        db.session.add(kp)
    kp.name, kp.phone, kp.vet_code, kp.tonnage = name, phone, vet, tonnage
    kp.destinations_json = json.dumps(destinations)
    kp.last_used = now

    db.session.commit()
    flash(f'نوبت شما با کد {to_fa(qcode)} ثبت شد.', 'ok')
    return redirect(url_for('driver_dashboard'))


# ---------- Routes: manager ----------
@app.route('/manager')
@login_required
@admin_required
def manager():
    maybe_cleanup()
    waiters = Driver.query.filter_by(status='waiting').order_by(Driver.created_at).all()
    loadeds = Driver.query.filter_by(status='loaded').order_by(Driver.loaded_at.desc()).all()
    destinations = Destination.query.order_by(Destination.name).all()
    return render_template('manager.html',
                           waiters=waiters, loadeds=loadeds,
                           destinations=destinations)


@app.route('/manager/add', methods=['POST'])
@login_required
@admin_required
def manager_add():
    name = request.form.get('name', '').strip()
    phone = fa_to_en(request.form.get('phone', '').strip())
    vet = fa_to_en(request.form.get('vet_code', '').strip())
    tonnage = fa_to_en(request.form.get('tonnage', '').strip())
    p_two = request.form.get('plate_two', '').strip()
    p_three = request.form.get('plate_three', '').strip()
    p_city = request.form.get('plate_city', '').strip()
    destinations = request.form.getlist('destinations')[:3]

    if not (name and phone and vet and tonnage and p_two and p_three and p_city):
        flash('همه فیلدها را پر کنید.', 'error')
        return redirect(url_for('manager'))
    if not vet.isdigit():
        flash('کد دامپزشکی باید فقط عدد باشد.', 'error')
        return redirect(url_for('manager'))
    if not destinations:
        flash('حداقل یک مقصد انتخاب کنید.', 'error')
        return redirect(url_for('manager'))

    pk = f'{p_two}-{p_three}-{p_city}'
    for d in Driver.query.filter_by(status='waiting').all():
        if d.plate_key == pk:
            flash('این پلاک هم‌اکنون در صف است.', 'error')
            return redirect(url_for('manager'))

    qcode = next_queue_code()
    now = datetime.utcnow()
    entry = Driver(
        queue_code=qcode,
        name=name, phone=phone, vet_code=vet, tonnage=tonnage,
        plate_two=p_two, plate_three=p_three, plate_city=p_city,
        destinations_json=json.dumps(destinations),
        status='waiting', created_at=now, timer_start_at=now,
    )
    db.session.add(entry)

    kp = db.session.get(KnownPlate, pk)
    if not kp:
        kp = KnownPlate(plate_key=pk)
        db.session.add(kp)
    kp.name, kp.phone, kp.vet_code, kp.tonnage = name, phone, vet, tonnage
    kp.destinations_json = json.dumps(destinations)
    kp.last_used = now

    db.session.commit()
    flash(f'نوبت با کد {to_fa(qcode)} ثبت شد.', 'ok')
    return redirect(url_for('manager'))


@app.route('/manager/assign/<int:did>', methods=['POST'])
@login_required
@admin_required
def manager_assign(did):
    d = db.session.get(Driver, did)
    if not d:
        abort(404)
    d.status = 'loaded'
    d.loaded_at = datetime.utcnow()
    d.cargo_dest = request.form.get('cargo_dest', '').strip()
    d.cargo_name = request.form.get('cargo_name', '').strip()
    d.cargo_ton = fa_to_en(request.form.get('cargo_ton', '').strip())
    db.session.commit()
    flash('بار اختصاص یافت.', 'ok')
    return redirect(url_for('manager'))


@app.route('/manager/remove/<int:did>', methods=['POST'])
@login_required
@admin_required
def manager_remove(did):
    d = db.session.get(Driver, did)
    if d:
        db.session.delete(d)
        db.session.commit()
    return redirect(url_for('manager'))


@app.route('/manager/extend/<int:did>', methods=['POST'])
@login_required
@admin_required
def manager_extend(did):
    d = db.session.get(Driver, did)
    if d:
        d.timer_start_at = datetime.utcnow()
        db.session.commit()
    return redirect(url_for('manager'))


@app.route('/manager/back/<int:did>', methods=['POST'])
@login_required
@admin_required
def manager_back(did):
    d = db.session.get(Driver, did)
    if not d:
        abort(404)
    d.status = 'waiting'
    now = datetime.utcnow()
    d.created_at = now
    d.timer_start_at = now
    d.loaded_at = None
    d.cargo_dest = None
    d.cargo_name = None
    d.cargo_ton = None
    db.session.commit()
    return redirect(url_for('manager'))


@app.route('/manager/destinations', methods=['POST'])
@login_required
@admin_required
def manager_dest_add():
    name = request.form.get('name', '').strip()
    if name and not Destination.query.filter_by(name=name).first():
        db.session.add(Destination(name=name))
        db.session.commit()
    return redirect(url_for('manager'))


@app.route('/manager/destinations/<int:did>/delete', methods=['POST'])
@login_required
@admin_required
def manager_dest_del(did):
    d = db.session.get(Destination, did)
    if d:
        db.session.delete(d)
        db.session.commit()
    return redirect(url_for('manager'))


@app.route('/api/plate')
@login_required
@admin_required
def api_plate():
    two = request.args.get('two', '').strip()
    three = request.args.get('three', '').strip()
    city = request.args.get('city', '').strip()
    pk = f'{two}-{three}-{city}'
    kp = db.session.get(KnownPlate, pk)
    if not kp:
        return jsonify({'found': False})
    return jsonify({
        'found': True,
        'name': kp.name or '',
        'phone': kp.phone or '',
        'vet_code': kp.vet_code or '',
        'tonnage': kp.tonnage or '',
        'destinations': kp.get_destinations(),
    })


# ---------- Init DB ----------
DEFAULT_DESTS = [
    'تهران','شهریار','اسلامشهر','ورامین','کرج','فردیس','نظرآباد',
    'اصفهان','کاشان','نجف‌آباد','خمینی‌شهر','شیراز','مرودشت','جهرم','کازرون',
    'مشهد','نیشابور','سبزوار','تربت حیدریه','بجنورد','شیروان','اسفراین',
    'بیرجند','قائن','فردوس','تبریز','مراغه','مرند','اهر','ارومیه','خوی',
    'میاندوآب','بوکان','اردبیل','پارس‌آباد','مشگین‌شهر','رشت','بندر انزلی',
    'لاهیجان','آستارا','ساری','بابل','آمل','قائم‌شهر','نوشهر','گرگان',
    'گنبد کاووس','علی‌آباد','اهواز','آبادان','خرمشهر','دزفول','بندر ماهشهر',
    'کرمان','سیرجان','رفسنجان','بم','کرمانشاه','اسلام‌آباد غرب','هرسین',
    'خرم‌آباد','بروجرد','دورود','همدان','ملایر','نهاوند','اراک','ساوه','خمین',
    'قم','قزوین','تاکستان','آبیک','زنجان','ابهر','خرمدره','سمنان','شاهرود',
    'گرمسار','یزد','میبد','اردکان','بندرعباس','بندرلنگه','میناب','قشم',
    'بوشهر','برازجان','گناوه','زاهدان','زابل','چابهار','ایرانشهر','سنندج',
    'سقز','مریوان','ایلام','دهلران','آبدانان','شهرکرد','بروجن','فارسان',
    'یاسوج','دوگنبدان','دهدشت',
]


def init_db():
    with app.app_context():
        try:
            db.create_all()
            if Destination.query.count() == 0:
                for n in DEFAULT_DESTS:
                    db.session.add(Destination(name=n))
                db.session.commit()
            admin_user = os.environ.get('ADMIN_USER', 'admin')
            admin_pass = os.environ.get('ADMIN_PASS', 'admin123')
            if not User.query.filter_by(username=admin_user).first():
                u = User(username=admin_user, is_admin=True, name='مدیر بارگیری')
                u.set_password(admin_pass)
                db.session.add(u)
                db.session.commit()
                print(f'Admin user created: {admin_user} / {admin_pass}')
        except Exception as e:
            print(f'DB init error: {e}')


init_db()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
