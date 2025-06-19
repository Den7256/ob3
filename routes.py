import datetime
import os
from werkzeug.utils import secure_filename
from flask import render_template, request, redirect, url_for, send_from_directory, session, jsonify, abort, current_app
from extensions import auth, db
from models import User, Post, Message, File
from utils import get_chat_room_name, active_users, filesizeformat_filter
from auth import sync_ad_users


def init_routes(app, socketio):
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

    app.template_filter('filesizeformat')(filesizeformat_filter)

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
        online_user_ids = list(active_users.keys())
        current_user = User.query.get(session['user_id'])
        current_department = current_user.department if current_user else None
        users = User.query.filter_by(is_active=True).all()

        online_same_dept = []
        online_other_dept = []
        offline_same_dept = []
        offline_other_dept = []

        for user in users:
            if user.id in online_user_ids:
                if user.department == current_department:
                    online_same_dept.append(user)
                else:
                    online_other_dept.append(user)
            else:
                if user.department == current_department:
                    offline_same_dept.append(user)
                else:
                    offline_other_dept.append(user)

        online_same_dept.sort(key=lambda u: u.fullname)
        online_other_dept.sort(key=lambda u: u.fullname)
        offline_same_dept.sort(key=lambda u: u.fullname)
        offline_other_dept.sort(key=lambda u: u.fullname)

        sorted_users = online_same_dept + online_other_dept + offline_same_dept + offline_other_dept
        departments = set()
        for user in users:
            if user.department and user.department.strip():
                departments.add(user.department)
        departments = sorted(departments)

        return render_template('users.html',
                               users=sorted_users,
                               departments=departments,
                               online_user_ids=online_user_ids,
                               current_user=current_user)

    @app.route('/profile/<username>')
    @auth.login_required
    def user_profile(username):
        user = User.query.filter_by(username=username, is_active=True).first_or_404()
        online_user_ids = list(active_users.keys())
        return render_template(
            'profile.html',
            profile_user=user,
            online_user_ids=online_user_ids
        )

    @app.route('/chat/<int:user_id>')
    @auth.login_required
    def chat(user_id):
        current_user_id = session['user_id']
        current_user = User.query.get(current_user_id)
        recipient = User.query.get_or_404(user_id)

        sent_to = db.session.query(
            Message.recipient_id
        ).filter(
            Message.sender_id == current_user_id
        ).distinct()

        received_from = db.session.query(
            Message.sender_id
        ).filter(
            Message.recipient_id == current_user_id
        ).distinct()

        all_user_ids = {id for (id,) in sent_to} | {id for (id,) in received_from}

        conversations = []
        unread_counts = {}

        for user_id in all_user_ids:
            last_message = Message.query.filter(
                ((Message.sender_id == current_user_id) & (Message.recipient_id == user_id)) |
                ((Message.sender_id == user_id) & (Message.recipient_id == current_user_id))
            ).order_by(Message.timestamp.desc()).first()

            if last_message:
                user = User.query.get(user_id)
                conversations.append((last_message, user))
                count = Message.query.filter_by(
                    sender_id=user_id,
                    recipient_id=current_user_id,
                    is_read=False
                ).count()
                unread_counts[user_id] = count

        conversations.sort(key=lambda x: x[0].timestamp, reverse=True)

        unread_messages = Message.query.filter_by(
            sender_id=user_id,
            recipient_id=current_user_id,
            is_read=False
        ).all()

        for msg in unread_messages:
            msg.is_read = True
        db.session.commit()

        return render_template(
            'chat.html',
            recipient=recipient,
            conversations=conversations,
            unread_counts=unread_counts,
            current_user=current_user
        )

    @app.route('/user_info/<int:user_id>')
    @auth.login_required
    def get_user_info(user_id):
        user = User.query.get_or_404(user_id)
        return jsonify({
            'id': user.id,
            'username': user.username,
            'fullname': user.fullname,
            'email': user.email,
            'department': user.department,
            'position': user.position
        })

    @app.route('/chat/history/<int:recipient_id>', methods=['GET'])
    @auth.login_required
    def chat_history(recipient_id):
        messages = Message.query.filter(
            ((Message.sender_id == session['user_id']) & (Message.recipient_id == recipient_id)) |
            ((Message.sender_id == recipient_id) & (Message.recipient_id == session['user_id']))
        ).order_by(Message.timestamp.desc()).limit(100).all()

        result = []
        for msg in messages:
            message_data = {
                'id': msg.id,
                'content': msg.content,
                'timestamp': msg.timestamp.isoformat() + 'Z',
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

        return jsonify(result[::-1])

    @app.route('/inbox')
    @auth.login_required
    def inbox():
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
            last_message = Message.query.filter(
                ((Message.sender_id == session['user_id']) & (Message.recipient_id == user_id)) |
                ((Message.sender_id == user_id) & (Message.recipient_id == session['user_id']))
            ).order_by(Message.timestamp.desc()).first()

            if last_message:
                user = User.query.get(user_id)
                conversations.append((last_message, user))
                count = Message.query.filter_by(
                    sender_id=user_id,
                    recipient_id=session['user_id'],
                    is_read=False
                ).count()
                unread_counts[user_id] = count

        conversations.sort(key=lambda x: x[0].timestamp, reverse=True)

        return render_template('inbox.html', conversations=conversations, unread_counts=unread_counts)

    @app.route('/upload/<int:recipient_id>', methods=['POST'])
    @auth.login_required
    def upload_file(recipient_id):
        if 'file' not in request.files:
            return jsonify({'error': 'No file part'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No selected file'}), 400

        filename = secure_filename(file.filename)
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        filesize = os.path.getsize(filepath)

        message = Message(
            content=f"Отправлен файл: {filename}",
            sender_id=session['user_id'],
            recipient_id=recipient_id,
            is_read=False
        )
        db.session.add(message)
        db.session.flush()

        new_file = File(
            filename=filename,
            user_id=session['user_id'],
            message_id=message.id,
            filesize=filesize
        )
        db.session.add(new_file)
        db.session.commit()

        room = get_chat_room_name(session['user_id'], recipient_id)
        sender = User.query.get(session['user_id'])

        file_data = {
            'id': message.id,
            'sender_id': session['user_id'],
            'recipient_id': recipient_id,
            'sender_name': sender.fullname,
            'content': f"Отправлен файл: {filename}",
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
            'room': room,
            'is_read': False,
            'files': [{
                'id': new_file.id,
                'filename': filename,
                'filesize': filesize
            }]
        }

        socketio.emit('new_message', file_data, room=room)

        recipient_info = active_users.get(recipient_id)
        is_recipient_online = recipient_info and recipient_info.get('room') == room

        if is_recipient_online:
            socketio.emit('new_message_notification', {
                'sender_id': session['user_id'],
                'sender_name': sender.fullname,
                'content': f"Отправлен файл: {filename}",
                'room': room,
                'recipient_id': recipient_id,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
            }, room=f"user_{recipient_id}")
        else:
            print(f"🔕 Пользователь {recipient_id} не в текущем чате или не онлайн")

        socketio.emit('inbox_update', {
            'user_id': session['user_id'],
            'recipient_id': recipient_id,
            'content': f"Отправлен файл: {filename}",
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
        }, room=f"user_{session['user_id']}")

        if recipient_id in active_users:
            socketio.emit('inbox_update', {
                'user_id': recipient_id,
                'sender_id': session['user_id'],
                'content': f"Отправлен файл: {filename}",
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
            }, room=f"user_{recipient_id}")

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
            message = file.message

            allowed = False

            if file.user_id == session['user_id']:
                allowed = True
            elif message and message.recipient_id == session['user_id']:
                allowed = True
            elif session.get('is_admin'):
                allowed = True

            if not allowed:
                return "Forbidden", 403

            file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], file.filename)

            if not os.path.exists(file_path):
                return "File not found", 404

            return send_from_directory(
                current_app.config['UPLOAD_FOLDER'],
                file.filename,
                as_attachment=True,
                download_name=file.filename
            )
        except Exception as e:
            return "Internal server error", 500

    @app.route('/mark_as_read/<int:message_id>', methods=['POST'])
    @auth.login_required
    def mark_as_read(message_id):
        try:
            message = Message.query.get_or_404(message_id)
            current_user_id = session['user_id']

            if message.recipient_id == current_user_id:
                message.is_read = True
                db.session.commit()

                room = get_chat_room_name(message.sender_id, message.recipient_id)
                socketio.emit('message_read', {
                    'message_id': message_id,
                    'is_read': True
                }, room=room)

                socketio.emit('inbox_update', {
                    'user_id': current_user_id,
                    'sender_id': message.sender_id,
                    'is_read_update': True
                }, room=f"user_{current_user_id}")

                return jsonify({'status': 'success'})

            return jsonify({
                'status': 'error',
                'message': 'Forbidden: Вы не получатель этого сообщения'
            }), 403
        except Exception as e:
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

    @app.route('/mark_all_as_read/<int:sender_id>', methods=['POST'])
    @auth.login_required
    def mark_all_as_read(sender_id):
        try:
            current_user_id = session['user_id']
            messages = Message.query.filter_by(
                sender_id=sender_id,
                recipient_id=current_user_id,
                is_read=False
            ).all()

            for msg in messages:
                msg.is_read = True

            db.session.commit()

            socketio.emit('inbox_update', {
                'user_id': current_user_id,
                'sender_id': sender_id,
                'is_read_update': True
            }, room=f"user_{current_user_id}")

            return jsonify({'status': 'success'})

        except Exception as e:
            db.session.rollback()
            return jsonify({'status': 'error'}), 500

    @app.route('/send_message', methods=['POST'])
    @auth.login_required
    def send_message_http():
        try:
            data = request.json
            recipient_id = data['recipient_id']
            content = data['content'].strip()

            if not content:
                return jsonify({'status': 'error', 'message': 'Empty message'}), 400

            message = Message(
                content=content,
                sender_id=session['user_id'],
                recipient_id=recipient_id,
                is_read=False
            )
            db.session.add(message)
            db.session.commit()

            room = get_chat_room_name(session['user_id'], recipient_id)
            sender = User.query.get(session['user_id'])

            message_data = {
                'id': message.id,
                'sender_id': session['user_id'],
                'recipient_id': recipient_id,
                'sender_name': sender.fullname,
                'content': content,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
                'room': room,
                'is_read': False,
                'files': []
            }

            socketio.emit('new_message', message_data, room=room)

            if recipient_id in active_users:
                socketio.emit('new_message_notification', {
                    'sender_id': session['user_id'],
                    'sender_name': sender.fullname,
                    'content': content,
                    'room': room,
                    'recipient_id': recipient_id,
                    'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
                }, room=f"user_{recipient_id}")

            socketio.emit('inbox_update', {
                'user_id': session['user_id'],
                'recipient_id': recipient_id,
                'content': content,
                'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
            }, room=f"user_{session['user_id']}")

            if recipient_id in active_users:
                socketio.emit('inbox_update', {
                    'user_id': recipient_id,
                    'sender_id': session['user_id'],
                    'content': content,
                    'timestamp': datetime.datetime.utcnow().isoformat() + 'Z'
                }, room=f"user_{recipient_id}")

            return jsonify({
                'status': 'success',
                'message_id': message.id
            })

        except Exception as e:
            db.session.rollback()
            return jsonify({'status': 'error', 'message': str(e)}), 500

    @app.cli.command('sync-ad')
    def sync_ad_users_command():
        sync_ad_users()
        print("Синхронизация завершена")