import eventlet
eventlet.monkey_patch()  # High-performance networking for SocketIO

from flask import Flask, request, render_template, send_from_directory
from flask_socketio import SocketIO
import os

# 1. INITIALIZE FLASK
# Explicitly setting static_folder ensures your logo.png is found on Render
app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'sutton_macro_secret'

# 2. SOCKET ENGINE
# cors_allowed_origins="*" allows TradingView to send data to this server
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

@app.route('/')
def index():
    """Serves the Master Terminal (Macro and Secret layers)."""
    return render_template('index.html')

# 3. LOGO BRIDGE
# This route specifically tells the server where to find your logo
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Smart Webhook: Automatically routes data to the Public or Private 
    layer based on the presence of the 'sec_card' key.
    """
    try:
        data = request.get_json()
        
        # Log to the console so you can see alerts in your Render logs
        print(f">>> SUTTON INCOMING DATA: {data}")
        
        # SMART ROUTE:
        # If alert contains "sec_card", it goes to the Private Indicator layer.
        # Otherwise, it updates the Public Macro cards.
        if data and 'sec_card' in data:
            socketio.emit('secret_update', data)
            print(">>> ROUTE: PRIVATE LAYER")
        else:
            socketio.emit('macro_update', data)
            print(">>> ROUTE: PUBLIC LAYER")
        
        return "SUCCESS", 200

    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return str(e), 400

if __name__ == '__main__':
    # Dynamic port for Render deployment
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
