from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# --- MASTER DATA STORE ---
# Initializing with baseline values (Current Sahm reading 0.35)
market_data = {
    "ratio": 1.1,         # Used for UI theme color (Gold vs Equity)
    "sma": 1.0,           
    "count": 0,           # Will display either rCount or fCount
    "regime_name": "Trend Transition",
    "momentum": "STABLE", # For the 3rd box
    "sahm": 0.35          # Your current calibrated reading
}

@app.route('/')
def index():
    # Pass all live data to the HTML template
    return render_template('index.html', 
                           ratio=market_data["ratio"], 
                           sma=market_data["sma"], 
                           count=int(market_data["count"]),
                           regime_name=market_data["regime_name"],
                           momentum=market_data["momentum"],
                           sahm=market_data["sahm"])

# --- TRADINGVIEW WEBHOOK ENDPOINT ---
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # Retrieve the JSON message sent by Pine Script
        data = request.get_json()
        
        # Security check: Match the ticker name set in Pine Script
        if data.get("ticker") == "SUTTON_MACRO":
            
            # 1. Update Sahm Rule (For the LED and Recession Override)
            market_data["sahm"] = float(data.get("sahm", 0.35))
            
            # 2. Update Regime Name
            market_data["regime_name"] = data.get("cycle", "Neutral")
            
            # 3. Dynamic Lookback: Use rise count for Commodities, fall count for Stocks
            if "Commodities" in market_data["regime_name"]:
                market_data["count"] = data.get("rise", 0)
                market_data["ratio"] = 1.1  # Set theme to Gold
                market_data["sma"] = 1.0
            else:
                market_data["count"] = data.get("fall", 0)
                market_data["ratio"] = 0.9  # Set theme to Equity Blue
                market_data["sma"] = 1.0

            # 4. Optional: Capture Momentum if added to Pine Script later
            if "momentum" in data:
                market_data["momentum"] = data.get("momentum")

            print(f"Data Received: {market_data['regime_name']} | Sahm: {market_data['sahm']}")
            
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"Webhook processing error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == '__main__':
    # host='0.0.0.0' allows external access from TradingView servers
    # port=5000 is the default; ensure this is open on your firewall
    app.run(host='0.0.0.0', port=5000, debug=True)
