import os
import datetime
from sqlalchemy import inspect
from flask import current_app
from extensions import db
from models import File

# Глобальный словарь для отслеживания активных пользователей
active_users = {}


def get_chat_room_name(user1_id, user2_id):
    id1 = int(user1_id)
    id2 = int(user2_id)
    sorted_ids = sorted([id1, id2])
    return f"chat_{sorted_ids[0]}_{sorted_ids[1]}"


def update_online_users(socketio):
    """Отправляет обновленный список онлайн-пользователей всем клиентам"""
    online_user_ids = list(active_users.keys())
    from models import User
    online_users = User.query.filter(User.id.in_(online_user_ids)).all()

    users_data = [{
        'id': user.id,
        'username': user.username,
        'fullname': user.fullname,
        'department': user.department or '',
        'position': user.position or ''
    } for user in online_users]

    socketio.emit('online_users_update', {'users': users_data, 'online_ids': online_user_ids})


def init_database(db, app):
    with app.app_context():
        db.create_all()
        inspector = inspect(db.engine)

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


def cleanup_old_files(app):
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


def filesizeformat_filter(value):
    if value is None or value == 0:
        return "0 B"

    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"