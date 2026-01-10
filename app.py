from flask import Flask, request, render_template, url_for
import os

app = Flask(__name__)

# System Memory - Initialized with default values to prevent 500 errors
data_storage = {
    "rise": 0,
    "fall": 0,
    "ratio": 1.0,      # Current Ratio (e.g. SPY/TLT)
    "sma": 1.0,        # 50 SMA of Ratio
    "count": 0,        # Days in current cycle
    "momentum": "STABLE", 
    "sahm": 0.00,
    "prev_dist": 0     # Internal tracker for Growing/Shrinking
}

@app.route('/')
def index():
    # Passing the dictionary keys as variables to index.html
    return render_template('index.html', **data_storage)

@app.route('/webhook', methods=['POST'])
def webhook():
    global data_storage
    data = request.json
    if data:
        # 1. Update basic fields
        for key in ["rise", "fall", "ratio", "sma", "count", "sahm"]:
            if key in data:
                data_storage[key] = float(data[key])
        
        # 2. Calculate Momentum (Growing vs Shrinking)
        # Distance = How far the Ratio is from the SMA
        current_dist = abs(data_storage["ratio"] - data_storage["sma"])
        
        if current_dist > data_storage["prev_dist"]:
            data_storage["momentum"] = "GROWING"
        elif current_dist < data_storage["prev_dist"]:
            data_storage["momentum"] = "SHRINKING"
        else:
            data_storage["momentum"] = "STABLE"
            
        data_storage["prev_dist"] = current_dist
        
    return "Signal Received", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
