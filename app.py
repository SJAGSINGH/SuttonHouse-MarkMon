from flask import Flask, request, render_template, url_for
import os

app = Flask(__name__)

# System Memory V4.8
data_storage = {
    "rise": 0,
    "fall": 0,
    "cycle": "STABLE",
    "cycle_len": "0 DAYS",
    "ticker": "MARKMON-V4",
    "regime": "EQUITY",
    "sahm": 0.00
}

@app.route('/')
def index():
    return render_template('index.html', **data_storage)

@app.route('/webhook', methods=['POST'])
def webhook():
    global data_storage
    data = request.json
    if data:
        for key in data_storage:
            if key in data:
                data_storage[key] = data[key]
    return "Signal Received", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
