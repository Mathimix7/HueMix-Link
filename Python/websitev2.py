from flask import Flask, render_template, redirect, url_for, flash, request
from HueConfig import getWebsitePort
from apiView import apiView
from serversView import serversView
from buttonsView import buttonsView
from settingsView import settingsView

app = Flask(__name__)
app.config['SECRET_KEY'] = 'vgvv sfsa hwuqhasjjk'

app.register_blueprint(buttonsView, url_prefix='/buttons')
app.register_blueprint(serversView, url_prefix='/servers')
app.register_blueprint(settingsView, url_prefix='/settings')
app.register_blueprint(apiView, url_prefix='/api')

@app.route("/", methods=['POST', 'GET'])
def main():
    return render_template('main.html')

app.run(host="0.0.0.0", port=getWebsitePort(), debug=True)