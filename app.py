from flask import Flask, render_template, request
import os

app = Flask(__name__)

# This holds your "MarkMon" data in memory
macro_data = {
    "rise": "0",
    "fall": "0",
    "cycle": "Waiting for Signal...",
    "ticker": "GOLD_STOCKS"
}

@app.route('/')
def index():
    # This sends the data to your webpage
    return render_template('index.html', **macro_data)

@app.route('/webhook', methods=['POST'])
def webhook():
    global macro_data
    data = request.json
    if data:
        # Update the numbers and the cycle name from TradingView
        macro_data.update(data)
    return {"status": "success"}, 200

if __name__ == "__main__":
    # Render binds the app to 0.0.0.0 and a specific port
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
