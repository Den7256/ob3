import datetime
from flask import session, request
from flask_socketio import emit, join_room, leave_room
from extensions import db
from models import User, Message
from utils import active_users, get_chat_room_name, update_online_users

def init_socket_handlers(socketio):
    @socketio.on('connect')
    def handle_connect(auth=None):
        print(f"⚡️ Новое подключение: {request.sid}")
        if 'user_id' in session:
            user_id = session['user_id']
            active_users[user_id] = {
                'sid': request.sid,
                'room': None
            }
            join_room(f"user_{user_id}")
            print(f"👤 Пользователь {user_id} подключен. SID: {request.sid}")
            emit('connection_success', {'message': 'Успешное подключение к WebSocket'})
            update_online_users(socketio)
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
            update_online_users(socketio)

    @socketio.on('leave_room')
    def handle_leave_room(data):
        room = data.get('room')
        if room:
            leave_room(room)
            print(f"👤 Пользователь {session.get('user_id', 'unknown')} вышел из комнаты: {room}")

    @socketio.on('join_chat')
    def handle_join_chat(data):
        if 'user_id' not in session:
            return

        user_id = session['user_id']
        recipient_id = data['recipient_id']

        try:
            room = data.get('room', get_chat_room_name(user_id, recipient_id))

            if user_id in active_users:
                active_users[user_id]['room'] = room
            else:
                active_users[user_id] = {
                    'sid': request.sid,
                    'room': room
                }

            print(f"👤 Пользователь {user_id} присоединился к комнате чата: {room}")
            join_room(room)
            emit('room_joined', {'room': room})
        except Exception as e:
            print(f"❌ Ошибка при присоединении к чату: {str(e)}")

    @socketio.on('update_presence')
    def handle_update_presence(data):
        user_id = session.get('user_id')
        if not user_id:
            return

        recipient_id = data['recipient_id']
        room = get_chat_room_name(user_id, recipient_id)

        if user_id in active_users:
            active_users[user_id]['room'] = room
        else:
            active_users[user_id] = {
                'sid': request.sid,
                'room': room
            }

        join_room(room)
        print(f"🔄 Пользователь {user_id} обновил присутствие в комнате: {room}")

    @socketio.on('send_message')
    def handle_send_message(data):
        print(f"✉️ Получено сообщение: {data}")
        try:
            if 'user_id' not in session:
                print("🚫 Попытка отправить сообщение без сессии")
                emit('error', {'message': 'Требуется аутентификации'}, room=request.sid)
                return

            user_id = session['user_id']
            recipient_id = data['recipient_id']
            content = data['content'].strip()
            temp_id = data.get('temp_id')

            if not content:
                print("⚪️ Пустое сообщение, пропускаем")
                return

            print(f"✉️ Сообщение от {user_id} для {recipient_id}: {content[:50]}...")

            message = Message(
                content=content,
                sender_id=user_id,
                recipient_id=recipient_id,
                is_read=False
            )
            db.session.add(message)
            db.session.commit()
            print(f"📝 Сообщение создано в БД, ID: {message.id}")

            room = get_chat_room_name(user_id, recipient_id)
            sender = User.query.get(user_id)

            message_data = {
                'id': message.id,
                'sender_id': user_id,
                'recipient_id': recipient_id,
                'sender_name': sender.fullname,
                'content': content,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
                'room': room,
                'is_read': False,
                'files': []
            }

            emit('new_message', message_data, room=room)
            print(f"📤 Сообщение отправлено в комнату: {room}")

            notification_data = {
                'sender_id': user_id,
                'sender_name': sender.fullname,
                'content': content,
                'room': room,
                'message_id': message.id,
                'recipient_id': recipient_id,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
            }

            recipient_info = active_users.get(recipient_id)
            is_recipient_online = recipient_info and recipient_info.get('room') == room

            if is_recipient_online:
                emit('new_message_notification', notification_data, room=f"user_{recipient_id}")
                print(f"🔔 Уведомление отправлено пользователю {recipient_id}")
            else:
                print(f"🔕 Пользователь {recipient_id} не в текущем чате или не онлайн")

            emit('message_delivered', {
                'temp_id': temp_id,
                'message_id': message.id,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
            }, room=request.sid)

            emit('inbox_update', {
                'user_id': user_id,
                'recipient_id': recipient_id,
                'content': content,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
            }, room=f"user_{user_id}")

            if recipient_id in active_users:
                emit('inbox_update', {
                    'user_id': recipient_id,
                    'sender_id': user_id,
                    'content': content,
                    'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
                }, room=f"user_{recipient_id}")

            print(f"✅ Сообщение успешно отправлено и сохранено")

        except Exception as e:
            db.session.rollback()
            print(f"❌ Ошибка при отправке сообщения: {str(e)}")
            import traceback
            traceback.print_exc()
            emit('error', {'message': 'Не удалось отправить сообщение'}, room=request.sid)
