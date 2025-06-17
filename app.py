# app.py
# -*- coding: utf-8 -*-
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
from sqlalchemy import ForeignKey
from sqlalchemy.orm import relationship
from flask import abort
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask import copy_current_request_context

# Игнорируем предупреждения об устаревших методах
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Загрузка конфигурации
load_dotenv()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', 'supersecretkey'),
    SESSION_COOKIE_SECURE=False,  # Для локальной разработки
    SESSION_COOKIE_SAMESITE='Lax',
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
    LDAP_SERVICE_PASSWORD=os.getenv('LDAP_SERVICE_PASSWORD'),
)

db = SQLAlchemy(app)
auth = HTTPBasicAuth()

# Инициализация SocketIO с улучшенными параметрами стабильности
socketio = SocketIO(
    app,
    async_mode='eventlet',
    cors_allowed_origins="*",
    logger=True,
    engineio_logger=True,
    ping_timeout=300,
    ping_interval=60,
    max_http_buffer_size=100 * 1024 * 1024,  # 100MB для файлов
    manage_session=False  # Важно для работы с сессиями
)

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
    last_seen = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    password_hash = db.Column(db.String(128), default='')

    def unread_messages_count(self):
        return Message.query.filter_by(
            recipient_id=self.id,
            is_read=False
        ).count()


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('posts', lazy=True))


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, index=True, default=datetime.datetime.utcnow)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    files = db.relationship('File', backref='message', lazy=True)
    sender = db.relationship('User', foreign_keys=[sender_id], backref=db.backref('sent_messages', lazy=True))
    recipient = db.relationship('User', foreign_keys=[recipient_id], backref=db.backref('received_messages', lazy=True))


class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    upload_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=True)
    filesize = db.Column(db.BigInteger, default=0)  # Добавлено поле для хранения размера файла
    user = db.relationship('User', backref=db.backref('files', lazy=True))

    @property
    def filepath(self):
        return os.path.join(app.config['UPLOAD_FOLDER'], self.filename)


# Глобальный словарь для отслеживания активных пользователей
active_users = {}  # user_id: sid


# Функция для получения имени комнаты чата
def get_chat_room_name(user1_id, user2_id):
    sorted_ids = sorted([user1_id, user2_id])
    return f"chat_{sorted_ids[0]}_{sorted_ids[1]}"


# Обработчики WebSocket
@socketio.on('connect')
def handle_connect(auth=None):  # Исправление: добавлен параметр auth
    print(f"⚡️ Новое подключение: {request.sid}")
    if 'user_id' in session:
        user_id = session['user_id']
        active_users[user_id] = request.sid
        join_room(f"user_{user_id}")  # Присоединяем к личной комнате для уведомлений
        print(f"👤 Пользователь {user_id} подключен. SID: {request.sid}")
        emit('connection_success', {'message': 'Успешное подключение к WebSocket'})

        # Отправляем обновленный список активных пользователей
        update_online_users()
    else:
        print("⚠️ Подключение без аутентификации")
        emit('reconnect_required', {'reason': 'Требуется аутентификация'})


@socketio.on('disconnect')
def handle_disconnect():
    print(f"❌ Отключение: {request.sid}")
    if 'user_id' in session:
        user_id = session['user_id']
        if user_id in active_users:
            del active_users[user_id]
        print(f"👤 Пользователь {user_id} отключен")

        # Отправляем обновленный список активных пользователей
        update_online_users()


@socketio.on('join_chat')
def handle_join_chat(data):
    if 'user_id' not in session:
        print("🚫 Попытка присоединиться к чату без сессии")
        emit('error', {'message': 'Требуется аутентификация'})
        return

    user_id = session['user_id']
    recipient_id = data['recipient_id']
    room = get_chat_room_name(user_id, recipient_id)
    join_room(room)
    print(f"👥 Пользователь {user_id} присоединился к комнате чата: {room}")
    emit('room_joined', {'room': room})


@socketio.on('send_message')
def handle_send_message(data):
    print(f"✉️ Получено сообщение: {data}")
    try:
        if 'user_id' not in session:
            print("🚫 Попытка отправить сообщение без сессии")
            emit('error', {'message': 'Требуется аутентификация'}, room=request.sid)
            return

        user_id = session['user_id']
        recipient_id = data['recipient_id']
        content = data['content'].strip()

        if not content:
            print("⚪️ Пустое сообщение, пропускаем")
            return

        print(f"✉️ Сообщение от {user_id} для {recipient_id}: {content[:50]}...")

        # Создаем сообщение в БД
        message = Message(
            content=content,
            sender_id=user_id,
            recipient_id=recipient_id,
            is_read=False
        )
        db.session.add(message)
        db.session.commit()  # Фиксируем сразу, чтобы получить ID
        print(f"📝 Сообщение создано в БД, ID: {message.id}")

        # Формируем комнату
        room = get_chat_room_name(user_id, recipient_id)

        # Получаем данные отправителя
        sender = User.query.get(user_id)

        # Формируем данные для отправки
        message_data = {
            'id': message.id,
            'sender_id': user_id,
            'recipient_id': recipient_id,
            'sender_name': sender.fullname,
            'content': content,
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',  # Добавляем 'Z' для UTC
            'room': room,
            'is_read': False,
            'files': []
        }

        # Отправляем сообщение в комнату чата
        emit('new_message', message_data, room=room)
        print(f"📤 Сообщение отправлено в комнату: {room}")

        # Отправляем уведомление в личную комнату получателя
        notification_data = {
            'sender_id': user_id,
            'sender_name': sender.fullname,
            'content': content,
            'room': room,
            'message_id': message.id,
            'recipient_id': recipient_id,
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
        }

        # Проверяем, что получатель онлайн
        if recipient_id in active_users:
            emit('new_message_notification', notification_data, room=f"user_{recipient_id}")
            print(f"🔔 Уведомление отправлено пользователю {recipient_id}")
        else:
            print(f"🔕 Пользователь {recipient_id} не в сети, уведомление не отправлено")

        # Отправляем подтверждение отправителю
        emit('message_delivered', {
            'message_id': message.id,
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
        }, room=request.sid)

        print(f"✅ Сообщение успешно отправлено и сохранено")

    except Exception as e:
        db.session.rollback()
        print(f"❌ Ошибка при отправке сообщения: {str(e)}")
        import traceback
        traceback.print_exc()
        emit('error', {'message': 'Не удалось отправить сообщение'}, room=request.sid)


def init_database():
    with app.app_context():
        db.create_all()
        inspector = db.inspect(db.engine)

        # Проверяем и добавляем отсутствующие столбцы
        tables = {
            'message': ['is_read'],
            'file': ['message_id', 'filesize']
        }

        for table_name, columns in tables.items():
            if table_name in inspector.get_table_names():
                existing_columns = [col['name'] for col in inspector.get_columns(table_name)]
                for column in columns:
                    if column not in existing_columns:
                        try:
                            with db.engine.begin() as connection:
                                if column == 'is_read':
                                    connection.execute(
                                        f"ALTER TABLE {table_name} ADD COLUMN {column} BOOLEAN DEFAULT 0")
                                elif column == 'filesize':
                                    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} BIGINT DEFAULT 0")
                                else:
                                    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column} INTEGER")
                            print(f"✅ Добавлен столбец {column} в таблицу {table_name}")
                        except Exception as e:
                            print(f"❌ Ошибка добавления столбца {column}: {str(e)}")

        print("🛢️ Инициализация схемы базы данных завершена")


def update_online_users():
    """Отправляет обновленный список онлайн-пользователей всем клиентам"""
    online_user_ids = list(active_users.keys())
    online_users = User.query.filter(User.id.in_(online_user_ids)).all()

    # Формируем список пользователей с базовой информацией
    users_data = [{
        'id': user.id,
        'username': user.username,
        'fullname': user.fullname
    } for user in online_users]

    # ИСПРАВЛЕНИЕ: используем правильный метод для широковещательной рассылки
    socketio.emit('online_users_update', {'users': users_data}, namespace='/')
    print(f"🔄 Отправлен обновленный список онлайн-пользователей: {len(online_users)} пользователей")


@app.context_processor
def inject_common_data():
    common = {
        'now': datetime.datetime.utcnow(),
        'current_year': datetime.datetime.utcnow().year
    }

    if 'user_id' in session:
        user = User.query.get(session['user_id'])
        common['current_user'] = user
        common['unread_messages_count'] = user.unread_messages_count() if user else 0

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

        formats = [
            f"{username}@{app.config['LDAP_DOMAIN']}",
            f"{app.config['LDAP_DOMAIN']}\\{username}",
            f"CN={username},{app.config['LDAP_USER_OU']}"
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
                print(f"⚠️ Ошибка SIMPLE аутентификации: {str(e)}")

        raise Exception("❌ Все попытки аутентификации не удались")
    else:
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
                print(f"⚠️ Ошибка аутентификации пользователя: {str(e)}")
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
        print(f"⚠️ Ошибка поиска в LDAP: {str(e)}")
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
        print(f"⚠️ Ошибка проверки группы LDAP: {str(e)}")
        return False


@auth.verify_password
def verify_password(username, password):
    try:
        conn = get_ldap_connection(username, password)
        if not conn or not conn.bound:
            print(f"⚠️ Не удалось подключиться к LDAP для пользователя {username}")
            return None

        ldap_user = get_user_ldap_attributes(username,
                                             ['displayName', 'mail', 'department', 'title'])

        if not ldap_user:
            print(f"⚠️ Пользователь {username} не найден в LDAP")
            return None

        user = User.query.filter_by(username=username).first()
        if not user:
            user = User(username=username)
            db.session.add(user)

        user.fullname = getattr(ldap_user, 'displayName', username)
        user.email = getattr(ldap_user, 'mail', '')
        user.department = getattr(ldap_user, 'department', '')
        user.position = getattr(ldap_user, 'title', '')
        user.is_active = True
        user.last_seen = datetime.datetime.utcnow()

        db.session.commit()

        session['user_id'] = user.id
        session['is_admin'] = is_user_in_group(username, app.config['LDAP_ADMIN_GROUP'])

        print(f"✅ Успешная аутентификация: {username}")
        return username
    except Exception as e:
        print(f"❌ Ошибка аутентификации: {str(e)}")
        return None


@app.cli.command('sync-ad')
def sync_ad_users():
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

            inactive_users = User.query.filter(User.username.notin_(active_users)).all()
            for user in inactive_users:
                user.is_active = False

            db.session.commit()
            print(f"✅ Синхронизировано {len(active_users)} пользователей")
    except Exception as e:
        print(f"❌ Ошибка синхронизации: {str(e)}")
        db.session.rollback()


def cleanup_old_files():
    with app.app_context():
        expiration = datetime.datetime.utcnow() - datetime.timedelta(days=app.config['FILE_LIFETIME'])
        old_files = File.query.filter(File.upload_date < expiration).all()

        deleted_count = 0
        for file in old_files:
            try:
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
                if os.path.exists(file_path):
                    os.remove(file_path)
                    print(f"🗑️ Удален файл: {file.filename}")
                db.session.delete(file)
                deleted_count += 1
            except OSError as e:
                print(f"⚠️ Ошибка удаления файла {file.filename}: {str(e)}")

        db.session.commit()
        print(f"🧹 Очищено {deleted_count} старых файлов")


@app.template_filter('filesizeformat')
def filesizeformat_filter(value):
    if value is None or value == 0:
        return "0 B"

    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


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

    # Помечаем входящие сообщения как прочитанные
    unread_messages = Message.query.filter_by(
        sender_id=user_id,
        recipient_id=session['user_id'],
        is_read=False
    ).all()

    for msg in unread_messages:
        msg.is_read = True

    db.session.commit()

    # Получаем все сообщения для этого чата
    messages = Message.query.filter(
        ((Message.sender_id == session['user_id']) & (Message.recipient_id == user_id)) |
        ((Message.sender_id == user_id) & (Message.recipient_id == session['user_id']))
    ).order_by(Message.timestamp.asc()).all()

    return render_template('chat.html', recipient=recipient, messages=messages)


@app.route('/chat/history/<int:recipient_id>', methods=['GET'])
@auth.login_required
def chat_history(recipient_id):
    print(f"📜 Загрузка истории чата для пользователя {session['user_id']} с {recipient_id}")

    # Получаем все сообщения для этого чата
    messages = Message.query.filter(
        ((Message.sender_id == session['user_id']) & (Message.recipient_id == recipient_id)) |
        ((Message.sender_id == recipient_id) & (Message.recipient_id == session['user_id']))
    ).order_by(Message.timestamp.desc()).limit(100).all()

    # Форматируем сообщения для JSON
    result = []
    for msg in messages:
        message_data = {
            'id': msg.id,
            'content': msg.content,
            'timestamp': msg.timestamp.isoformat() + 'Z',  # Добавляем 'Z' для UTC
            'sender_id': msg.sender_id,
            'sender_name': msg.sender.fullname,
            'is_read': msg.is_read,
            'files': [{
                'id': f.id,
                'filename': f.filename,
                'filesize': f.filesize
            } for f in msg.files]
        }
        result.append(message_data)

    print(f"📚 Найдено {len(result)} сообщений")
    return jsonify(result[::-1])  # Переворачиваем, чтобы старые сообщения были первыми


@app.route('/inbox')
@auth.login_required
def inbox():
    # Получаем ID всех собеседников
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

            # Считаем непрочитанные сообщения от этого пользователя
            count = Message.query.filter_by(
                sender_id=user_id,
                recipient_id=session['user_id'],
                is_read=False
            ).count()
            unread_counts[user_id] = count

    # Сортируем по времени последнего сообщения
    conversations.sort(key=lambda x: x[0].timestamp, reverse=True)

    return render_template('inbox.html', conversations=conversations, unread_counts=unread_counts)


@app.route('/upload/<int:recipient_id>', methods=['POST'])
@auth.login_required
def upload_file(recipient_id):
    """Отдельный роут для загрузки файлов в чате"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    filesize = os.path.getsize(filepath)

    # Создаем сообщение для файла
    message = Message(
        content=f"Отправлен файл: {filename}",
        sender_id=session['user_id'],
        recipient_id=recipient_id,
        is_read=False
    )
    db.session.add(message)
    db.session.flush()  # Получаем ID сообщения

    new_file = File(
        filename=filename,
        user_id=session['user_id'],
        message_id=message.id,
        filesize=filesize
    )
    db.session.add(new_file)
    db.session.commit()

    # Отправляем уведомление о новом файле
    room = get_chat_room_name(session['user_id'], recipient_id)
    sender = User.query.get(session['user_id'])

    # Формируем данные для уведомления
    file_data = {
        'id': message.id,
        'sender_id': session['user_id'],
        'recipient_id': recipient_id,
        'sender_name': sender.fullname,
        'content': f"Отправлен файл: {filename}",
        'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',  # Добавляем 'Z' для UTC
        'room': room,
        'is_read': False,
        'files': [{
            'id': new_file.id,
            'filename': filename,
            'filesize': filesize
        }]
    }

    # Отправляем сообщение в комнату чата
    socketio.emit('new_message', file_data, room=room)

    # Отправляем уведомление только получателю, если он онлайн
    if recipient_id in active_users:
        socketio.emit('new_message_notification', {
            'sender_id': session['user_id'],
            'sender_name': sender.fullname,
            'content': f"Отправлен файл: {filename}",
            'room': room,
            'recipient_id': recipient_id,
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
        }, room=f"user_{recipient_id}")
        print(f"🔔 Уведомление о файле отправлено пользователю {recipient_id}")
    else:
        print(f"🔕 Пользователь {recipient_id} не в сети, уведомление не отправлено")

    return jsonify({
        'success': True,
        'message_id': message.id,
        'file_id': new_file.id
    })


@app.route('/download/<int:file_id>')
@auth.login_required
def download_file(file_id):
    try:
        file = File.query.get_or_404(file_id)
        message = file.message  # Связанное сообщение

        # Проверяем права доступа:
        # 1. Отправитель файла
        # 2. Получатель сообщения
        # 3. Администратор
        allowed = False

        if file.user_id == session['user_id']:
            allowed = True  # Отправитель файла

        elif message and message.recipient_id == session['user_id']:
            allowed = True  # Получатель сообщения

        elif session.get('is_admin'):
            allowed = True  # Администратор

        if not allowed:
            print(f"🚫 Доступ запрещен к файлу {file_id} для пользователя {session['user_id']}")
            return "Forbidden", 403

        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)

        if not os.path.exists(file_path):
            print(f"⚠️ Файл не найден: {file_path}")
            return "File not found", 404

        print(f"📥 Отправка файла: {file.filename} пользователю {session['user_id']}")
        return send_from_directory(
            app.config['UPLOAD_FOLDER'],
            file.filename,
            as_attachment=True,
            download_name=file.filename
        )
    except Exception as e:
        print(f"❌ Ошибка при скачивании файла {file_id}: {str(e)}")
        return "Internal server error", 500


@app.route('/mark_as_read/<int:message_id>', methods=['POST'])
@auth.login_required
def mark_as_read(message_id):
    try:
        message = Message.query.get_or_404(message_id)
        current_user_id = session['user_id']

        # Проверяем, что текущий пользователь - получатель
        if message.recipient_id == current_user_id:
            message.is_read = True
            db.session.commit()
            print(f"✅ Сообщение {message_id} помечено как прочитанное пользователем {current_user_id}")
            return jsonify({'status': 'success'})

        print(f"🚫 Попытка пометить чужое сообщение: "
              f"message_id={message_id}, recipient={message.recipient_id}, "
              f"current_user={current_user_id}")

        return jsonify({
            'status': 'error',
            'message': 'Forbidden: Вы не получатель этого сообщения'
        }), 403
    except Exception as e:
        print(f"❌ Ошибка при пометке сообщения {message_id}: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': 'Internal server error'
        }), 500


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


@app.route('/unread_count')
@auth.login_required
def unread_count():
    count = Message.query.filter_by(
        recipient_id=session['user_id'],
        is_read=False
    ).count()
    return jsonify({'count': count})


@app.route('/send_message', methods=['POST'])
@auth.login_required
def send_message_http():
    """Эндпоинт для отправки сообщений через HTTP"""
    try:
        data = request.json
        recipient_id = data['recipient_id']
        content = data['content'].strip()

        if not content:
            return jsonify({'status': 'error', 'message': 'Empty message'}), 400

        # Создаем сообщение в БД
        message = Message(
            content=content,
            sender_id=session['user_id'],
            recipient_id=recipient_id,
            is_read=False
        )
        db.session.add(message)
        db.session.commit()

        # Формируем комнату
        room = get_chat_room_name(session['user_id'], recipient_id)
        sender = User.query.get(session['user_id'])

        # Формируем данные для отправки
        message_data = {
            'id': message.id,
            'sender_id': session['user_id'],
            'recipient_id': recipient_id,
            'sender_name': sender.fullname,
            'content': content,
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',  # Добавляем 'Z' для UTC
            'room': room,
            'is_read': False,
            'files': []
        }

        # Отправляем через WebSocket
        socketio.emit('new_message', message_data, room=room)

        # Отправляем уведомление только получателю, если он онлайн
        if recipient_id in active_users:
            socketio.emit('new_message_notification', {
                'sender_id': session['user_id'],
                'sender_name': sender.fullname,
                'content': content,
                'room': room,
                'recipient_id': recipient_id,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
            }, room=f"user_{recipient_id}")
            print(f"🔔 Уведомление отправлено пользователю {recipient_id}")
        else:
            print(f"🔕 Пользователь {recipient_id} не в сети, уведомление не отправлено")

        return jsonify({
            'status': 'success',
            'message_id': message.id
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error sending message: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Инициализация приложения
def init_app():
    # Инициализация базы данных
    init_database()

    # Очистка старых файлов в контексте приложения
    with app.app_context():
        cleanup_old_files()
    print("🚀 Приложение инициализировано")


# Запуск приложения
if __name__ == '__main__':
    init_app()
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=True,
        use_reloader=False,  # Важно для стабильности WebSocket
        allow_unsafe_werkzeug=True
    )