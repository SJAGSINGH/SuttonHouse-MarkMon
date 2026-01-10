from flask import Flask, request, render_template
from flask_socketio import SocketIO, emit
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sutton_secret'
# This allows the dashboard to listen for live updates
socketio = SocketIO(app, cors_allowed_origins="*")

# 1. Route to serve the HTML Dashboard
@app.route('/')
def index():
    return render_template('index.html')

# 2. The Webhook Endpoint (Where TradingView sends data)
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == 'POST':
        data = request.data.decode('utf-8')
        try:
            # Parse the incoming JSON from TradingView
            json_data = json.loads(data)
            
            # Print to Render logs for debugging
            print(f"Received Data: {json_data}")
            
            # 3. Push data to the Dashboard via WebSockets
            socketio.emit('macro_update', json_data)
            
            return "Webhook Received", 200
        except Exception as e:
            print(f"Error parsing JSON: {e}")
            return "Error", 400
    else:
        return "Invalid Method", 405

if __name__ == '__main__':
    # Use the port Render provides
    import os
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
