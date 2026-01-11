from flask import Flask, request, render_template, send_from_directory
from flask_socketio import SocketIO
import os

app = Flask(__name__, static_folder='static')

# 'threading' is the only mode that works with gthread on Render
# allow_unsafe_werkzeug=True is needed for the latest Flask versions
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
        print(f"Incoming Webhook: {data}") # This helps us see it in the logs
        
        # We use namespace='/' and broadcast=True to ensure the screen updates
        socketio.emit('macro_update', data, namespace='/', broadcast=True)
        
        return "SUCCESS", 200
    except Exception as e:
        print(f"Error: {e}")
        return str(e), 400

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    socketio.run(app, host='0.0.0.0', port=port)
