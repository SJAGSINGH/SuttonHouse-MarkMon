from flask import Flask, request, render_template, url_for
import os

app = Flask(__name__)

# System Memory V4.5 - Initialized for Sutton House
data_storage = {
    "rise": 0,
    "fall": 0,
    "cycle": "AWAITING",
    "cycle_len": "0 DAYS",
    "ticker": "SUTTON-MONITOR",
    "regime": "EQUITY",
    "sahm": 0.00
}

@app.route('/')
def index():
    # Pass all variables from data_storage to index.html
    return render_template('index.html', **data_storage)

@app.route('/webhook', methods=['POST'])
def webhook():
    global data_storage
    data = request.json
    
    # Log incoming data to Render "Logs" for troubleshooting
    print(f"[INCOMING SIGNAL]: {data}")
    
    if data:
        # Update memory only if the key exists in our data_storage
        for key in data_storage:
            if key in data:
                data_storage[key] = data[key]
                
    return "Signal Received", 200

if __name__ == '__main__':
    # Render uses the PORT environment variable
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
