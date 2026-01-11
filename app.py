import eventlet
eventlet.monkey_patch()

from flask import Flask, request, render_template, send_from_directory
from flask_socketio import SocketIO
import os

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'sutton_macro_secret'

# Note: explicitly setting async_mode to 'eventlet' is critical for Render
socketio = SocketIO(app, 
                   cors_allowed_origins="*", 
                   async_mode='eventlet', 
                   logger=False, 
                   engineio_logger=False)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        print(f"DEBUG: Datum Received -> {data}") # Check your Render logs for this!
        
        def emit_task(payload):
            # A tiny sleep ensures the eventlet loop is ready to broadcast
            eventlet.sleep(0.1) 
            if payload and 'sec_card' in payload:
                socketio.emit('secret_update', payload)
            else:
                socketio.emit('macro_update', payload)
            print("DEBUG: Datum Emitted to Socket")

        socketio.start_background_task(emit_task, data)
        return "SUCCESS", 200

    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return str(e), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
