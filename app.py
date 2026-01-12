from flask import Flask, request, render_template, send_from_directory
from flask_socketio import SocketIO
import os

app = Flask(__name__, static_folder='static')

# Use threading mode to remain compatible with Gunicorn gthreads
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

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
        
        # This print statement is vital for your Render logs
        print(f"Incoming Webhook: {data}") 
        
        # FIX: Removed 'broadcast=True' and 'namespace' to avoid the 400 error.
        # socketio.emit broadcasts to everyone by default.
        socketio.emit('macro_update', data)
        
        return "SUCCESS", 200
    except Exception as e:
        print(f"Error in webhook: {e}")
        return str(e), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    socketio.run(app, host='0.0.0.0', port=port)
