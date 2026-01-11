import eventlet
eventlet.monkey_patch()

from flask import Flask, request, render_template, send_from_directory
from flask_socketio import SocketIO
import os

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'sutton_macro_secret'

# Optimized for Render: SocketIO emits to all connected clients by default
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
        
        # We removed 'broadcast=True' because socketio.emit() 
        # handles broadcasting to all clients automatically here.
        if data and 'sec_card' in data:
            socketio.emit('secret_update', data)
        else:
            socketio.emit('macro_update', data)
            
        return "SUCCESS", 200
    except Exception as e:
        # This will show up in your Render logs if something fails
        print(f"WEBHOOK ERROR: {e}")
        return str(e), 400

if __name__ == '__main__':
    # Render provides the PORT environment variable
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
