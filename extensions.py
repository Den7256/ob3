from flask_sqlalchemy import SQLAlchemy
from flask_httpauth import HTTPBasicAuth
from flask_socketio import SocketIO

db = SQLAlchemy()
auth = HTTPBasicAuth()
socketio = SocketIO()