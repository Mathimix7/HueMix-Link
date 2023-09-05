from flask import Blueprint, render_template, redirect, url_for, flash, request
from HueConfig import discoverBridge, saveBridge, updateAPI, getPorts, savePorts 
import requests

settingsView = Blueprint('settingsView', __name__)

@settingsView.route("/", methods=['POST', 'GET'])
def settings():
    if request.method == 'POST':
        if request.form['buttons'] == 'Set-Up HueBridge':
            return redirect(url_for('settingsView.bridges'))
        elif request.form['buttons'] == "Set-Up Ports":
            return redirect(url_for('settingsView.setupports'))
    return render_template('settings.html')

@settingsView.route("/ports", methods=['POST', 'GET'])
def setupports():
    tcpPort, websitePort = getPorts()
    if request.method == 'POST':
        if request.form['buttons'] == 'Save':
            if len(request.form.get('websitePort')) != 0:
                if not request.form.get('websitePort').isnumeric():
                    flash(u'Error saving new ports, most likely due to incorrect port number!', 'error')
                    return redirect(url_for('settingsView.settings'))
                websitePort = request.form.get('websitePort')                
            if len(request.form.get('TCPPort')) != 0:
                if not request.form.get('TCPPort').isnumeric():
                    flash(u'Error saving new ports, most likely due to incorrect port number!', 'error')
                    return redirect(url_for('settingsView.settings'))
                tcpPort = request.form.get('TCPPort')
            savePorts(tcpPort, websitePort)
            flash(u'Changes saved successfully!', 'success')
            return redirect(url_for('settingsView.settings'))
        elif request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('settingsView.settings'))
    return render_template('setupports.html', tcpPort=tcpPort, websitePort=websitePort)

@settingsView.route("/discover")
def bridges():
    try:
        ip = requests.get("https://discovery.meethue.com/").json()[0]["internalipaddress"]
    except:
        try:
            ip = discoverBridge()
        except:
            ip = None
    ip = None
    if ip != None:
        return redirect(f"/settings/setup?ip={ip}")
    return render_template('discoverBridge.html')

@settingsView.route("/discoverManual")
def manualDiscover():
    ip = request.args.get("ip")
    try:
        a = requests.get(url=f"http://{ip}/api/newdeveloper").json()
        if a == [{"error":{"type":1,"address":"/","description":"unauthorized user"}}]:
            return redirect(f"/settings/setup?ip={ip}")
        else:
            raise
    except:
        return render_template("discoverManual.html")

@settingsView.route("/setup")
def setup():
    return render_template('setupBridge.html')

@settingsView.route("/createUser")
def createUser():
    ip = request.args.get("ip")
    try:
        a = requests.post(url=f"http://{ip}/api", json={"devicetype":"HueMixLink"}).json()
    except:
        flash("Unexpected Error!", "error")
        return render_template(url_for("settingsView.settings"))
    try:
        if a[0]['error']["description"] == "link button not pressed":
            return render_template("pressButton.html")
    except Exception as e:
        if a[0]["success"]:
            token = a[0]["success"]["username"]
            saveBridge(ip, token)
            flash("Succesfully saved new bridge!", "success")
            updateAPI()
            return redirect("settingsView.settings")
        else:
            flash("Unexpected Error!", "error")
            return render_template(url_for("settingsView.settings"))