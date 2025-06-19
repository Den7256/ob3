import os
import warnings
from flask import Flask
from dotenv import load_dotenv
from extensions import db, auth, socketio
from utils import init_database, cleanup_old_files
from routes import init_routes
from sockets import init_socket_handlers

# Игнорируем предупреждения
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Загрузка конфигурации
load_dotenv()

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY', 'supersecretkey'),
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_SAMESITE='Lax',
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URI', 'sqlite:///social.db'),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER', 'uploads'),
    MAX_CONTENT_LENGTH=int(os.getenv('MAX_FILE_SIZE', 16 * 1024 * 1024)),
    FILE_LIFETIME=int(os.getenv('FILE_LIFETIME', 7)),
    LDAP_SERVER=os.getenv('LDAP_SERVER'),
    LDAP_DOMAIN=os.getenv('LDAP_DOMAIN'),
    LDAP_SEARCH_BASE=os.getenv('LDAP_SEARCH_BASE'),
    LDAP_USER_OU=os.getenv('LDAP_USER_OU'),
    LDAP_ADMIN_GROUP=os.getenv('LDAP_ADMIN_GROUP'),
    LDAP_SERVICE_ACCOUNT=os.getenv('LDAP_SERVICE_ACCOUNT'),
    LDAP_SERVICE_PASSWORD=os.getenv('LDAP_SERVICE_PASSWORD'),
)

# Инициализация расширений
db.init_app(app)
socketio.init_app(
    app,
    async_mode='eventlet',
    cors_allowed_origins="*",
    logger=True,
    engineio_logger=True,
    ping_timeout=300,
    ping_interval=60,
    max_http_buffer_size=100 * 1024 * 1024,
    manage_session=False
)

# Создаем папку для загрузок
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Инициализация компонентов
init_socket_handlers(socketio)
init_routes(app, socketio)

# Инициализация приложения
def init_app():
    init_database(db, app)
    with app.app_context():
        cleanup_old_files(app)
    print("🚀 Приложение инициализировано")

# Запуск приложения
if __name__ == '__main__':
    init_app()
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=True,
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )