import os
import datetime
import sqlite3
import warnings
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_httpauth import HTTPBasicAuth
from werkzeug.utils import secure_filename
from ldap3 import Server, Connection, ALL, NTLM, SIMPLE
from sqlalchemy import func, desc

# Игнорируем предупреждения об устаревших методах
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Загрузка конфигурации
load_dotenv()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', 'supersecretkey'),
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URI', 'sqlite:///social.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER', 'uploads'),
    MAX_CONTENT_LENGTH=int(os.getenv('MAX_FILE_SIZE', 16 * 1024 * 1024)),
    FILE_LIFETIME=int(os.getenv('FILE_LIFETIME', 7)),
    # LDAP Configuration
    LDAP_SERVER=os.getenv('LDAP_SERVER'),
    LDAP_DOMAIN=os.getenv('LDAP_DOMAIN'),
    LDAP_SEARCH_BASE=os.getenv('LDAP_SEARCH_BASE'),
    LDAP_USER_OU=os.getenv('LDAP_USER_OU'),
    LDAP_ADMIN_GROUP=os.getenv('LDAP_ADMIN_GROUP'),
    LDAP_SERVICE_ACCOUNT=os.getenv('LDAP_SERVICE_ACCOUNT'),
    LDAP_SERVICE_PASSWORD=os.getenv('LDAP_SERVICE_PASSWORD')
)

db = SQLAlchemy(app)
auth = HTTPBasicAuth()

# Создаем папку для загрузок
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# Модели базы данных
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    fullname = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120))
    department = db.Column(db.String(120))
    position = db.Column(db.String(120))
    is_active = db.Column(db.Boolean, default=True)
    last_seen = db.Column(db.DateTime, default=datetime.datetime.now)
    password_hash = db.Column(db.String(128), default='')

    def unread_messages_count(self):
        return Message.query.filter_by(
            recipient_id=self.id,
            is_read=False
        ).count()


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.datetime.now)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('posts', lazy=True))


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.datetime.now)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    sender = db.relationship('User', foreign_keys=[sender_id], backref=db.backref('sent_messages', lazy=True))
    recipient = db.relationship('User', foreign_keys=[recipient_id], backref=db.backref('received_messages', lazy=True))


class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    upload_date = db.Column(db.DateTime, default=datetime.datetime.now)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('files', lazy=True))


# Гарантированное создание/обновление схемы БД
def init_database():
    with app.app_context():
        # Создаем все таблицы, если их нет
        db.create_all()

        # Проверяем существующие столбцы в таблице message
        inspector = db.inspect(db.engine)

        if 'message' in inspector.get_table_names():
            columns = [col['name'] for col in inspector.get_columns('message')]

            # Добавляем отсутствующие столбцы
            if 'is_read' not in columns:
                try:
                    with db.engine.begin() as connection:
                        connection.execute("ALTER TABLE message ADD COLUMN is_read BOOLEAN DEFAULT 0")
                    app.logger.info("Added column: is_read to message")
                except Exception as e:
                    app.logger.error(f"Error adding column is_read: {str(e)}")

        app.logger.info("Database schema initialized")


# Контекстный процессор для добавления общих данных во все шаблоны
@app.context_processor
def inject_common_data():
    common = {
        'now': datetime.datetime.now(),
        'current_year': datetime.datetime.now().year
    }

    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        common['current_user'] = user

        # Подсчет непрочитанных сообщений для навбара
        common['unread_messages_count'] = Message.query.filter_by(
            recipient_id=session['user_id'],
            is_read=False
        ).count()

    return common


# LDAP Helper Functions
def get_ldap_connection(username=None, password=None, service_auth=False):
    server = Server(
        app.config['LDAP_SERVER'],
        get_info=ALL,
        connect_timeout=5
    )

    if service_auth:
        username = app.config['LDAP_SERVICE_ACCOUNT']
        password = app.config['LDAP_SERVICE_PASSWORD']

        # Форматы для подключения
        formats = [
            f"{username}@{app.config['LDAP_DOMAIN']}",  # UPN формат
            f"{app.config['LDAP_DOMAIN']}\\{username}",  # DOMAIN\\username
            f"CN={username},{app.config['LDAP_USER_OU']}"  # Distinguished Name
        ]

        for user_dn in formats:
            try:
                conn = Connection(
                    server,
                    user=user_dn,
                    password=password,
                    authentication=NTLM,
                    auto_bind=True
                )
                if conn.bound:
                    return conn
            except Exception:
                continue

        # Если NTLM не сработал, пробуем SIMPLE
        for user_dn in formats:
            try:
                conn = Connection(
                    server,
                    user=user_dn,
                    password=password,
                    authentication=SIMPLE,
                    auto_bind=True
                )
                if conn.bound:
                    return conn
            except Exception as e:
                app.logger.warning(f"SIMPLE auth failed: {str(e)}")

        raise Exception("All authentication attempts failed")
    else:
        # Для обычных пользователей
        user_dn = f"{username}@{app.config['LDAP_DOMAIN']}"
        try:
            return Connection(
                server,
                user=user_dn,
                password=password,
                authentication=NTLM,
                auto_bind=True
            )
        except Exception:
            try:
                return Connection(
                    server,
                    user=user_dn,
                    password=password,
                    authentication=SIMPLE,
                    auto_bind=True
                )
            except Exception as e:
                app.logger.error(f"User auth failed: {str(e)}")
                return None


def get_user_ldap_attributes(username, attributes):
    try:
        conn = get_ldap_connection(service_auth=True)
        search_filter = f"(sAMAccountName={username})"
        conn.search(
            app.config['LDAP_SEARCH_BASE'],
            search_filter,
            attributes=attributes
        )
        if conn.entries:
            return conn.entries[0]
        return None
    except Exception as e:
        app.logger.error(f"LDAP search failed: {str(e)}")
        return None


def is_user_in_group(username, group_dn):
    try:
        user_attrs = get_user_ldap_attributes(username, ['distinguishedName'])
        if not user_attrs or not hasattr(user_attrs, 'distinguishedName'):
            return False

        user_dn = user_attrs.distinguishedName.value

        conn = get_ldap_connection(service_auth=True)
        search_filter = f"(&(objectClass=group)(distinguishedName={group_dn})(member={user_dn}))"
        conn.search(
            app.config['LDAP_SEARCH_BASE'],
            search_filter,
            attributes=['cn']
        )
        return bool(conn.entries)
    except Exception as e:
        app.logger.error(f"LDAP group check failed: {str(e)}")
        return False


@auth.verify_password
def verify_password(username, password):
    try:
        # Пытаемся подключиться к AD
        conn = get_ldap_connection(username, password)
        if not conn or not conn.bound:
            return None

        # Получаем информацию о пользователе
        ldap_user = get_user_ldap_attributes(username,
                                             ['displayName', 'mail', 'department', 'title'])

        if not ldap_user:
            return None

        # Ищем или создаем пользователя в локальной БД
        user = User.query.filter_by(username=username).first()
        if not user:
            user = User(username=username)
            db.session.add(user)

        # Обновляем атрибуты
        user.fullname = getattr(ldap_user, 'displayName', username)
        user.email = getattr(ldap_user, 'mail', '')
        user.department = getattr(ldap_user, 'department', '')
        user.position = getattr(ldap_user, 'title', '')
        user.is_active = True
        user.last_seen = datetime.datetime.now()

        db.session.commit()

        # Сохраняем в сессии
        session['user_id'] = user.id
        session['is_admin'] = is_user_in_group(username, app.config['LDAP_ADMIN_GROUP'])

        return username
    except Exception as e:
        app.logger.error(f"Authentication failed: {str(e)}")
        return None


# CLI Commands
@app.cli.command('sync-ad')
def sync_ad_users():
    """Синхронизация пользователей с Active Directory"""
    try:
        with app.app_context():
            conn = get_ldap_connection(service_auth=True)
            conn.search(
                app.config['LDAP_USER_OU'],
                '(objectClass=user)',
                attributes=['sAMAccountName', 'displayName', 'mail', 'department', 'title']
            )

            active_users = []
            for entry in conn.entries:
                username = entry.sAMAccountName.value
                active_users.append(username)

                user = User.query.filter_by(username=username).first()
                if not user:
                    user = User(username=username)
                    db.session.add(user)

                user.fullname = entry.displayName.value
                user.email = entry.mail.value if 'mail' in entry and entry.mail.value else ''
                user.department = entry.department.value if 'department' in entry and entry.department.value else ''
                user.position = entry.title.value if 'title' in entry and entry.title.value else ''
                user.is_active = True

            # Деактивируем отсутствующих в AD пользователей
            inactive_users = User.query.filter(User.username.notin_(active_users)).all()
            for user in inactive_users:
                user.is_active = False

            db.session.commit()
            print(f"Синхронизировано {len(active_users)} пользователей")
    except Exception as e:
        print(f"Ошибка синхронизации: {str(e)}")
        db.session.rollback()


# Utility Functions
def cleanup_old_files():
    with app.app_context():
        # Используем datetime.now() вместо устаревшего utcnow()
        expiration = datetime.datetime.now() - datetime.timedelta(days=app.config['FILE_LIFETIME'])
        old_files = File.query.filter(File.upload_date < expiration).all()

        for file in old_files:
            try:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError as e:
                app.logger.error(f"Error deleting file {file.filename}: {str(e)}")
            db.session.delete(file)

        db.session.commit()
        app.logger.info(f"Cleaned up {len(old_files)} old files")


# Routes
@app.route('/')
@auth.login_required
def index():
    posts = Post.query.order_by(Post.timestamp.desc()).all()
    return render_template('index.html', posts=posts)


@app.route('/post', methods=['POST'])
@auth.login_required
def create_post():
    content = request.form.get('content')
    if content:
        post = Post(content=content, user_id=session['user_id'])
        db.session.add(post)
        db.session.commit()
    return redirect(url_for('index'))


@app.route('/users')
@auth.login_required
def users():
    user_list = User.query.filter_by(is_active=True).all()
    return render_template('users.html', users=user_list)


@app.route('/profile/<username>')
@auth.login_required
def user_profile(username):
    user = User.query.filter_by(username=username, is_active=True).first_or_404()
    return render_template('profile.html', profile_user=user)


@app.route('/chat/<int:user_id>', methods=['GET', 'POST'])
@auth.login_required
def chat(user_id):
    recipient = User.query.get_or_404(user_id)

    if request.method == 'POST':
        content = request.form.get('content')
        if content:
            message = Message(
                content=content,
                sender_id=session['user_id'],
                recipient_id=user_id,
                is_read=False
            )
            db.session.add(message)
            db.session.commit()

    # Помечаем все сообщения от этого пользователя как прочитанные
    Message.query.filter_by(
        sender_id=user_id,
        recipient_id=session['user_id'],
        is_read=False
    ).update({'is_read': True})
    db.session.commit()

    messages = Message.query.filter(
        ((Message.sender_id == session['user_id']) & (Message.recipient_id == user_id)) |
        ((Message.sender_id == user_id) & (Message.recipient_id == session['user_id']))
    ).order_by(Message.timestamp.asc()).all()

    return render_template('chat.html', recipient=recipient, messages=messages)


@app.route('/inbox')
@auth.login_required
def inbox():
    # Получаем всех собеседников
    sent_to = db.session.query(
        Message.recipient_id
    ).filter(
        Message.sender_id == session['user_id']
    ).distinct()

    received_from = db.session.query(
        Message.sender_id
    ).filter(
        Message.recipient_id == session['user_id']
    ).distinct()

    all_user_ids = {id for (id,) in sent_to} | {id for (id,) in received_from}

    # Для каждого собеседника получаем последнее сообщение
    conversations = []
    unread_counts = {}

    for user_id in all_user_ids:
        # Получаем последнее сообщение в диалоге
        last_message = Message.query.filter(
            ((Message.sender_id == session['user_id']) & (Message.recipient_id == user_id)) |
            ((Message.sender_id == user_id) & (Message.recipient_id == session['user_id']))
        ).order_by(Message.timestamp.desc()).first()

        if last_message:
            user = User.query.get(user_id)
            conversations.append((last_message, user))

            # Считаем непрочитанные
            count = Message.query.filter_by(
                sender_id=user_id,
                recipient_id=session['user_id'],
                is_read=False
            ).count()
            unread_counts[user_id] = count

    # Сортируем по времени последнего сообщения
    conversations.sort(key=lambda x: x[0].timestamp, reverse=True)

    return render_template('inbox.html', conversations=conversations, unread_counts=unread_counts)


@app.route('/upload', methods=['POST'])
@auth.login_required
def upload_file():
    if 'file' not in request.files:
        return redirect(request.referrer)

    file = request.files['file']
    if file.filename == '':
        return redirect(request.referrer)

    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        new_file = File(
            filename=filename,
            user_id=session['user_id']
        )
        db.session.add(new_file)
        db.session.commit()

    return redirect(request.referrer)


@app.route('/download/<int:file_id>')
@auth.login_required
def download_file(file_id):
    file = File.query.get_or_404(file_id)
    return send_from_directory(
        app.config['UPLOAD_FOLDER'],
        file.filename,
        as_attachment=True
    )


@app.route('/admin')
@auth.login_required
def admin_panel():
    if not session.get('is_admin'):
        return "Forbidden", 403

    users = User.query.all()
    stats = {
        'total_users': User.query.count(),
        'active_users': User.query.filter_by(is_active=True).count(),
        'total_posts': Post.query.count(),
        'total_files': File.query.count(),
        'total_messages': Message.query.count()
    }
    return render_template('admin.html', users=users, stats=stats)


# Инициализация приложения
def init_app():
    # Инициализация базы данных
    init_database()

    # Очистка старых файлов в контексте приложения
    with app.app_context():
        cleanup_old_files()


# Запуск приложения
if __name__ == '__main__':
    init_app()
    app.run(host='0.0.0.0', port=5000, debug=True)