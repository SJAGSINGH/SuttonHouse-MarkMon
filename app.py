import eventlet
eventlet.monkey_patch()  # Must be the first line for high-performance networking

from flask import Flask, request, render_template
from flask_socketio import SocketIO
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sutton_macro_secret'

# SocketIO setup with CORS enabled so TradingView can "talk" to the dashboard
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

@app.route('/')
def index():
    """Serves the burnished gold dashboard."""
    return render_template('index.html')

@app.route('/webhook', methods=['POST'])
def webhook():
    """Receives the Pine Script v6 JSON payload."""
    try:
        data = request.get_json()
        
        # Log the incoming data to Render so you can verify the 0.35 Sahm reading
        print(f">>> SUTTON MACRO ALERT: {data}")
        
        # Immediate broadcast to your browser
        socketio.emit('macro_update', data)
        
        return "SUCCESS", 200
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return str(e), 400

if __name__ == '__main__':
    # Dynamic port selection for Render deployment
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
