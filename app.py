from flask import Flask, render_template
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY","dev")

@app.route("/")
def index():
    return render_template("index.html", brand="Pgym 2.0")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
