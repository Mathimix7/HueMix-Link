from flask import Flask, render_template, redirect, url_for, flash, request
import requests
from HueConfig import getRooms, getDevices, getScenes, checkRooms, checkMacAddress, ScenesIDtoName, confirmConfig, writeNewName, getServers, serversWriteNewName, removeServer, removeButton, discoverBridge, saveBridge, updateAPI, hueServer, getPorts, savePorts, getWebsitePort, getServerData, time_ago, getServerStatus

app = Flask(__name__)
app.config['SECRET_KEY'] = 'vgvv sfsa hwuqhasjjk'

@app.route("/", methods=['POST', 'GET'])
def main():
    return render_template('main.html')

@app.route("/settings", methods=['POST', 'GET'])
def settings():
    if request.method == 'POST':
        if request.form['buttons'] == 'Set-Up HueBridge':
            return redirect(url_for('bridges'))
        elif request.form['buttons'] == "Set-Up Ports":
            return redirect(url_for('setupports'))
    return render_template('settings.html')

@app.route("/setup/ports", methods=['POST', 'GET'])
def setupports():
    tcpPort, websitePort = getPorts()
    if request.method == 'POST':
        if request.form['buttons'] == 'Save':
            if len(request.form.get('websitePort')) != 0:
                if not request.form.get('websitePort').isnumeric():
                    flash(u'Error saving new ports, most likely due to incorrect port number!', 'error')
                    return redirect(url_for('settings'))
                websitePort = request.form.get('websitePort')                
            if len(request.form.get('TCPPort')) != 0:
                if not request.form.get('TCPPort').isnumeric():
                    flash(u'Error saving new ports, most likely due to incorrect port number!', 'error')
                    return redirect(url_for('settings'))
                tcpPort = request.form.get('TCPPort')
            savePorts(tcpPort, websitePort)
            flash(u'Changes saved successfully!', 'success')
            return redirect(url_for('settings'))
        elif request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('settings'))
    return render_template('setupports.html', tcpPort=tcpPort, websitePort=websitePort)

@app.route("/discover")
def bridges():
    try:
        ip = requests.get("https://discovery.meethue.com/").json()[0]["internalipaddress"]
    except:
        try:
            ip = discoverBridge()
        except:
            ip = None
    if ip != None:
        return redirect(f"/setup?ip={ip}")
    return render_template('discoverBridge.html')

@app.route("/discoverManual")
def manualDiscover():
    ip = request.args.get("ip")
    try:
        a = requests.get(url=f"http://{ip}/api/newdeveloper").json()
        if a == [{"error":{"type":1,"address":"/","description":"unauthorized user"}}]:
            return redirect(f"/setup?ip={ip}")
        else:
            raise
    except:
        return render_template("discoverManual.html")

@app.route("/setup")
def setup():
    return render_template('setupBridge.html')

@app.route("/createUser")
def createUser():
    ip = request.args.get("ip")
    try:
        a = requests.post(url=f"http://{ip}/api", json={"devicetype":"HueMixLink"}).json()
    except:
        flash("Unexpected Error!", "error")
        return render_template(url_for("settings"))
    try:
        if a[0]['error']["description"] == "link button not pressed":
            return render_template("pressButton.html")
    except Exception as e:
        if a[0]["success"]:
            token = a[0]["success"]["username"]
            saveBridge(ip, token)
            flash("Succesfully saved new bridge!", "success")
            updateAPI()
            return redirect("settings")
        else:
            flash("Unexpected Error!", "error")
            return render_template(url_for("settings"))
    
@app.route("/servers", methods=['POST', 'GET'])
def servers():
    try:
        if request.method == 'POST':
            if request.form['buttons'] == 'Delete':
                macAddress = request.form['rooms']
                removeServer(macAddress)
                flash(f'Successfully deleted server {macAddress.split("- ")[0]}!', 'success')
            if request.form['buttons'] == 'Rename':
                macAddress = request.form['rooms']
                return redirect(url_for(f'serverRename', macAddress=macAddress))
            if request.form['buttons'] == 'Information':
                macAddress = request.form['rooms']
                return redirect(url_for(f'serverinfo', macAddress=macAddress))
    except:
        flash(u'Please choose the server you want to modify!', 'error')
        return redirect(url_for('servers'))
    serversJSON = getServers()
    return render_template('servers.html', messages=serversJSON)

@app.route("/serverinfo", methods=['POST', 'GET'])
def serverinfo():
    macAddress = request.args['macAddress']
    onlyMacAddress = macAddress.split('- ')[1].split(' ')[0]
    serverData = getServerData(onlyMacAddress)
    serverData["lastUsed"] = time_ago(serverData["lastUsed"])
    status = getServerStatus(serverData["ip"])
    print(status)
    serverData.update(status)
    serverData["led_on_time"] = int(serverData["led_on_time"])
    serverData["led_off_time"] = int(serverData["led_off_time"])
    return render_template('serverinfo.html', data=serverData)

@app.route(f'/servers/rename', methods = ['POST', 'GET'])
def serverRename():
    try: 
        macAddress = request.args['macAddress']
        onlyMacAddress = macAddress.split('- ')[1].split(' ')[0]
        macs = []
        data = getServers()
        for device in data:
            macs.append(data[device].split('- ')[1].split(' ')[0])
        if not onlyMacAddress in macs:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'servers'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'servers'))
    if request.method == 'POST':
        if request.form['buttons'] == 'Change':
            if len(request.form.get('nname')) != 0:
                if '- ' in request.form.get('nname'):
                    flash(u'Invalid Name!', 'error')
                    return redirect(url_for(f'setupRename', macAddress=macAddress))
                serversWriteNewName(macAddress, request.form.get('nname'))
                flash(u'Changes saved successfully!', 'success')
                return redirect(url_for('servers'))
            else:
                flash(u'Name is too short!', 'error')
                return redirect(url_for(f'setupRename', macAddress=macAddress))
        elif request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('servers'))
    return render_template('setupRename.html')

@app.route(f'/buttons', methods=['POST', 'GET'])
def home():
    try:
        if request.method == 'POST':
            if request.form['buttons'] == 'Set-up Scenes':
                macAddress = request.form['rooms']
                if hueServer():
                    return redirect(url_for(f'setupRoom', macAddress=macAddress))
                else:
                    flash("No Hue Bridge found!", "error")
                    return redirect(url_for('settings'))
            elif request.form['buttons'] == 'Rename':
                macAddress = request.form['rooms']
                return redirect(url_for(f'setupRename', macAddress=macAddress))
            elif request.form['buttons'] == 'Delete':
                macAddress = request.form['rooms']
                removeButton(macAddress)
                flash(f'Successfully deleted button {macAddress.split("- ")[0]}!', 'success')
    except:
        flash(u'Please choose the button you want to modify!', 'error')
        return redirect(url_for('home'))
    DevicesList = getDevices()
    return render_template('home.html', messages=DevicesList)

@app.route(f'/setup/rename', methods = ['POST', 'GET'])
def setupRename():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'home'))
    if request.method == 'POST':
        if request.form['buttons'] == 'Change':
            if len(request.form.get('nname')) != 0:
                if '- ' in request.form.get('nname'):
                    flash(u'Invalid Name!', 'error')
                    return redirect(url_for(f'setupRename', macAddress=macAddress))
                writeNewName(macAddress, request.form.get('nname'))
                flash(u'Changes saved successfully!', 'success')
                return redirect(url_for('home'))
            else:
                flash(u'Name is too short!', 'error')
                return redirect(url_for(f'setupRename', macAddress=macAddress))
        elif request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('home'))
    return render_template('setupRename.html')

@app.route(f'/setup/rooms', methods=['POST', 'GET'])
def setupRoom():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'home'))
    RoomList,RoomID = getRooms()
    try:
        if request.method == 'POST':
            if request.form['buttons'] == 'Cancel':
                flash(u'Set-up cancelled successfully!', 'info')
                return redirect(url_for('home'))
            Room = request.form['rooms']
            return redirect(url_for(f'setupScenes', macAddress=macAddress, RoomID=RoomID[Room]))
    except: 
        flash(u'Please choose the room the button is in!', 'error')
        render_template('setupRoom.html', messages=macAddress)
    return render_template('setupRoom.html', messages=RoomList)

@app.route(f'/setup/scenes', methods=['POST', 'GET'])
def setupScenes():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'home'))
    try: 
        RoomID = request.args['RoomID']
        validRoomID = checkRooms(RoomID)
        if not validRoomID: 
            flash(u'Invalid Room!', 'error')
            return redirect(url_for(f'setupRoom', macAddress=macAddress))
    except: 
        flash(u'Invalid Room!', 'error')
        return redirect(url_for(f'setupRoom', macAddress=macAddress))
    SceneList, SceneCodeList = getScenes(RoomID)
    try:
        if request.method == 'POST': 
            if request.form['buttons'] == 'Cancel':
                flash(u'Set-up cancelled successfully!', 'info')
                return redirect(url_for('home'))
            ScenesSelectedScenes = request.form.getlist('scenes')
            ScenesCode = ""
            if len(ScenesSelectedScenes) < 2:
                flash(u'Please choose at least 2 scenes!', 'error')
                return redirect(url_for(f'setupScenes', macAddress=macAddress, RoomID=RoomID))
            for i in range(len(ScenesSelectedScenes)):
                ScenesCode = ScenesCode + f"{SceneCodeList[ScenesSelectedScenes[i]]},"
            return redirect(url_for(f'setupConfirm', macAddress=macAddress, RoomID=RoomID, SceneCodeList=ScenesCode[:-1]))
    except: 
        flash(u'Please choose at least 2 scenes!', 'error')
        return redirect(url_for(f'setupScenes', macAddress=macAddress, RoomID=RoomID))
    return render_template('setupScenes.html', messages=SceneList)

@app.route('/setup/confirm', methods=['POST', 'GET'])
def setupConfirm():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress: 
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'home'))
    try: 
        RoomID = request.args['RoomID']
        validRoomID = checkRooms(RoomID)
        if not validRoomID: 
            flash(u'Invalid Room!', 'error')
            return redirect(url_for(f'setupRoom', macAddress=macAddress))
    except: 
        flash(u'Invalid Room!', 'error')
        return redirect(url_for(f'setupRoom', macAddress=macAddress))
    try: 
        SceneCode = request.args['SceneCodeList']
        SceneCodesList = SceneCode.split(",")
    except: 
        flash(u'Invalid Scene(s)!', 'error')
        return redirect(url_for(f'setupScenes', macAddress=macAddress, RoomID=RoomID))
    SceneNamesList = ScenesIDtoName(SceneCodesList)
    if request.method == 'POST':
        if request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('home'))
        elif request.form['buttons'] == 'Confirm':
            try:
                confirmConfig(FullmacAddress=macAddress, RoomNumber=RoomID, listScene=SceneCodesList, listSceneName=SceneNamesList)
            except:
                flash(u'Invalid Scene(s)!', 'error')
                return redirect(url_for(f'setupScenes', macAddress=macAddress, RoomID=RoomID))
            flash(u'Changes saved successfully!', 'success')
            return redirect(url_for('home'))
    return render_template('setupConfirm.html', messages=SceneNamesList, macAddress=macAddress, SceneNamesList=",".join(list(SceneNamesList.values())))


app.run(host="0.0.0.0", port=getWebsitePort(), debug=True)