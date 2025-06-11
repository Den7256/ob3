import os
from dotenv import load_dotenv
from ldap3 import Server, Connection, ALL, NTLM

load_dotenv()


def test_ldap():
    server = os.getenv('LDAP_SERVER')
    domain = os.getenv('LDAP_DOMAIN')
    username = os.getenv('LDAP_SERVICE_ACCOUNT')
    password = os.getenv('LDAP_SERVICE_PASSWORD')

    print(f"Testing connection to: {server}")
    print(f"Credentials: {username}/{'*' * len(password)}")

    # Пробуем разные форматы
    formats = [
        (f"{username}@{domain}", "UPN"),
        (f"{domain}\\{username}", "Down-Level"),
        (f"CN={username},{os.getenv('LDAP_USER_OU')}", "DistinguishedName")
    ]

    for user_format, name in formats:
        try:
            print(f"\nTrying {name} format: {user_format}")
            conn = Connection(
                Server(server, get_info=ALL),
                user=user_format,
                password=password,
                authentication=NTLM,
                auto_bind=True
            )
            print("Success! Connection established")
            print(f"Server info: {conn.server.info}")
            return True
        except Exception as e:
            print(f"Error: {str(e)}")

    print("\nAll authentication attempts failed")
    return False


if __name__ == '__main__':
    test_ldap()