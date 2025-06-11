from app import db, app


def recreate_database():
    with app.app_context():
        # Удаляем все таблицы
        db.drop_all()

        # Создаем заново
        db.create_all()

        print("Database recreated successfully")


if __name__ == '__main__':
    recreate_database()