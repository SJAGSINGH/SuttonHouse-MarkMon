import eventlet
eventlet.monkey_patch()

from flask import Flask, request, render_template, send_from_directory
from flask_socketio import SocketIO
import os

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'sutton_macro_secret'

# Added logger=False and engineio_logger=False to prevent 
# excessive console blocking which often causes the RuntimeError on Render
socketio = SocketIO(app, 
                   cors_allowed_origins="*", 
                   async_mode='eventlet', 
                   logger=False, 
                   engineio_logger=False)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        
        # Use a non-blocking background task to emit if the 
        # main loop feels "clogged" by the incoming BTC alert
        if data and 'sec_card' in data:
            socketio.emit('secret_update', data)
        else:
            socketio.emit('macro_update', data)
        
        return "SUCCESS", 200

    except Exception as e:
        return str(e), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # We use socketio.run, but Render prefers gunicorn. 
    # Ensure your Render Start Command is: 
    # gunicorn --worker-class eventlet -w 1 app:app
    socketio.run(app, host='0.0.0.0', port=port)
