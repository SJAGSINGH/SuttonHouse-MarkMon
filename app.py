import eventlet
eventlet.monkey_patch() # Must be the first line

from flask import Flask, request, render_template
from flask_socketio import SocketIO, emit
import json
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sutton_macro_secret'

# Note: async_mode='eventlet' is required for Render
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        print(f"Data received: {data}")
        socketio.emit('macro_update', data)
        return "OK", 200
    except Exception as e:
        return str(e), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
