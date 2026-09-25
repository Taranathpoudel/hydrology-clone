"""Flask front end. Renders tiny Jinja templates; the browser talks to FastAPI."""
from flask import Flask, render_template

API_BASE = "http://127.0.0.1:8000"

app = Flask(__name__)
app.config["API_BASE"] = API_BASE


@app.context_processor
def inject_api():
    return {"api_base": API_BASE}


@app.route("/")
def index():
    return render_template("index.html", active="dashboard")


@app.route("/river-watch")
def river_watch():
    return render_template("river_watch.html", active="river")


@app.route("/data-watch")
def data_watch():
    return render_template("data_watch.html", active="data")


@app.route("/compare")
def compare():
    return render_template("compare.html", active="compare")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)