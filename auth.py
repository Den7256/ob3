# auth.py
import os
import datetime
from flask import session
from ldap3 import Server, Connection, ALL, NTLM, SIMPLE, SUBTREE
from extensions import auth, db
from models import User
from flask import current_app

def get_ldap_connection(username=None, password=None, service_auth=False):
    server = Server(
        current_app.config['LDAP_SERVER'],
        get_info=ALL,
        connect_timeout=5
    )

    if service_auth:
        username = current_app.config['LDAP_SERVICE_ACCOUNT']
        password = current_app.config['LDAP_SERVICE_PASSWORD']

        formats = [
            f"{username}@{current_app.config['LDAP_DOMAIN']}",
            f"{current_app.config['LDAP_DOMAIN']}\\{username}",
            f"CN={username},{current_app.config['LDAP_USER_OU']}"
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
        user_dn = f"{username}@{current_app.config['LDAP_DOMAIN']}"
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
            current_app.config['LDAP_SEARCH_BASE'],
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
            current_app.config['LDAP_SEARCH_BASE'],
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
        session['is_admin'] = is_user_in_group(username, current_app.config['LDAP_ADMIN_GROUP'])

        print(f"✅ Успешная аутентификация: {username}")
        return username
    except Exception as e:
        print(f"❌ Ошибка аутентификации: {str(e)}")
        return None

def get_ldap_attr(entry, attr_name, default=None):
    """Безопасное извлечение атрибута из LDAP-записи"""
    if hasattr(entry, attr_name) and getattr(entry, attr_name).value:
        return getattr(entry, attr_name).value
    return default

def sync_ad_users():
    try:
        with current_app.app_context():
            ous = current_app.config['LDAP_USER_OU'].split(';')
            active_users = []
            conn = get_ldap_connection(service_auth=True)

            for ou in ous:
                ou = ou.strip()
                if not ou:
                    continue

                try:
                    print(f"🔍 Поиск пользователей в OU: {ou}")
                    conn.search(
                        search_base=ou,
                        search_filter='(objectClass=user)',
                        attributes=['sAMAccountName', 'displayName', 'mail', 'department', 'title'],
                        search_scope=SUBTREE
                    )

                    for entry in conn.entries:
                        username = get_ldap_attr(entry, 'sAMAccountName')
                        if not username:
                            print(f"⚠️ Пропуск записи без sAMAccountName")
                            continue

                        if username in active_users:
                            print(f"⚠️ Пользователь {username} уже обработан, пропускаем дубликат")
                            continue

                        active_users.append(username)

                        user = User.query.filter_by(username=username).first()
                        if not user:
                            user = User(username=username)
                            db.session.add(user)
                            print(f"➕ Добавлен новый пользователь: {username}")

                        user.fullname = get_ldap_attr(entry, 'displayName', username)
                        user.email = get_ldap_attr(entry, 'mail', '')
                        user.department = get_ldap_attr(entry, 'department', '')
                        user.position = get_ldap_attr(entry, 'title', '')
                        user.is_active = True

                        print(f"🔄 Обновлен пользователь: {username} ({user.fullname})")

                except Exception as ou_error:
                    print(f"⚠️ Ошибка при обработке OU {ou}: {str(ou_error)}")
                    continue

            inactive_users = User.query.filter(User.username.notin_(active_users)).all()
            for user in inactive_users:
                user.is_active = False
                print(f"⏸️ Пользователь деактивирован: {user.username}")

            db.session.commit()
            print(f"✅ Синхронизация завершена. Пользователей: {len(active_users)}, "
                  f"Деактивировано: {len(inactive_users)}")

    except Exception as e:
        print(f"❌ Критическая ошибка синхронизации: {str(e)}")
        db.session.rollback()
        import traceback
        traceback.print_exc()