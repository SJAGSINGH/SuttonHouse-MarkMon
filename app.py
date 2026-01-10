from flask import Flask, request, render_template
import os

app = Flask(__name__)

# Initial system memory - Weekend V4.5
data_storage = {
    "rise": 0,
    "fall": 0,
    "cycle": "AWAITING DATA",
    "ticker": "MARKMON-V4",
    "regime": "EQUITY",
    "sahm": 0.0
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
    return "Data Received", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080)) # Matches your log port
    app.run(host='0.0.0.0', port=port)
