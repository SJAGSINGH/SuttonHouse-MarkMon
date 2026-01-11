import eventlet
eventlet.monkey_patch()

from flask import Flask, request, render_template, send_from_directory
from flask_socketio import SocketIO
import os

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'sutton_macro_secret'

# Keep loggers False to save resources on Render's free tier
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
        
        # Define the emission task
        def emit_task(payload):
            if payload and 'sec_card' in payload:
                socketio.emit('secret_update', payload)
            else:
                socketio.emit('macro_update', payload)

        # Start the task in a non-blocking green thread
        socketio.start_background_task(emit_task, data)
        
        return "SUCCESS", 200

    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return str(e), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
