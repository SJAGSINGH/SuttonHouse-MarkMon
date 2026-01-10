from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# --- GLOBAL DATA STORE ---
# These hold the values that the dashboard displays.
# They will be updated instantly when TradingView sends an alert.
market_data = {
    "ratio": 1.1,        # Placeholder for myRatio
    "sma": 1.0,          # Placeholder for sma50
    "count": 0,          # Will show rCount or fCount
    "regime_name": "Trend Transition",
    "momentum": "STABLE",
    "sahm": 0.35         # Your current Sahm reading
}

@app.route('/')
def index():
    # This sends the global data to your HTML file
    return render_template('index.html', 
                           ratio=market_data["ratio"], 
                           sma=market_data["sma"], 
                           count=int(market_data["count"]),
                           regime_name=market_data["regime_name"],
                           momentum=market_data["momentum"],
                           sahm=market_data["sahm"])

# --- THE WEBHOOK LISTENER ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        
        # Check if this is your SuttonHouse Macro alert
        if data.get("ticker") == "GOLD_STOCKS":
            # 1. Update Regime Name
            market_data["regime_name"] = data.get("cycle", "Neutral")
            
            # 2. Logic: If "Commodities" use rCount (rise), else use fCount (fall)
            if "Commodities" in market_data["regime_name"]:
                market_data["count"] = data.get("rise", 0)
                market_data["ratio"] = 1.1 # Force 'Commodity' theme color
                market_data["sma"] = 1.0
            else:
                market_data["count"] = data.get("fall", 0)
                market_data["ratio"] = 0.9 # Force 'Equity' theme color
                market_data["sma"] = 1.0

        # Optional: Listen for a separate Sahm alert if you set one up
        if "sahm_val" in data:
            market_data["sahm"] = data.get("sahm_val")

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"status": "error"}), 400

if __name__ == '__main__':
    # Use 0.0.0.0 to make it accessible to the internet (TradingView)
    app.run(host='0.0.0.0', port=5000, debug=True)
