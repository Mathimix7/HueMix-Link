from flask import Blueprint, render_template, redirect, url_for, flash, request
from HueConfig import getRooms, getDevices, getScenes, checkRooms, checkMacAddress, ScenesIDtoName, confirmConfig, writeNewName, removeButton, hueServer

buttonsView = Blueprint('buttonsView', __name__)

@buttonsView.route(f'/', methods=['POST', 'GET'])
def home():
    try:
        if request.method == 'POST':
            if request.form['buttons'] == 'Set-up Scenes':
                macAddress = request.form['rooms']
                if hueServer():
                    return redirect(url_for(f'buttonsView.setupRoom', macAddress=macAddress))
                else:
                    flash("No Hue Bridge found!", "error")
                    return redirect(url_for('settingsView.settings'))
            elif request.form['buttons'] == 'Rename':
                macAddress = request.form['rooms']
                return redirect(url_for(f'buttonsView.setupRename', macAddress=macAddress))
            elif request.form['buttons'] == 'Delete':
                macAddress = request.form['rooms']
                removeButton(macAddress)
                flash(f'Successfully deleted button {macAddress.split("- ")[0]}!', 'success')
    except:
        flash(u'Please choose the button you want to modify!', 'error')
        return redirect(url_for('buttonsView.home'))
    DevicesList = getDevices()
    return render_template('home.html', messages=DevicesList)

@buttonsView.route(f'/rename', methods = ['POST', 'GET'])
def setupRename():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'buttonsView.home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'buttonsView.home'))
    if request.method == 'POST':
        if request.form['buttons'] == 'Change':
            if len(request.form.get('nname')) != 0:
                if '- ' in request.form.get('nname'):
                    flash(u'Invalid Name!', 'error')
                    return redirect(url_for(f'buttonsView.setupRename', macAddress=macAddress))
                writeNewName(macAddress, request.form.get('nname'))
                flash(u'Changes saved successfully!', 'success')
                return redirect(url_for('buttonsView.home'))
            else:
                flash(u'Name is too short!', 'error')
                return redirect(url_for(f'buttonsView.setupRename', macAddress=macAddress))
        elif request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('buttonsView.home'))
    return render_template('setupRename.html')

@buttonsView.route(f'/rooms', methods=['POST', 'GET'])
def setupRoom():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'buttonsView.home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'buttonsView.home'))
    RoomList,RoomID = getRooms()
    try:
        if request.method == 'POST':
            if request.form['buttons'] == 'Cancel':
                flash(u'Set-up cancelled successfully!', 'info')
                return redirect(url_for('buttonsView.home'))
            Room = request.form['rooms']
            return redirect(url_for(f'buttonsView.setupScenes', macAddress=macAddress, RoomID=RoomID[Room]))
    except: 
        flash(u'Please choose the room the button is in!', 'error')
        render_template('setupRoom.html', messages=macAddress)
    return render_template('setupRoom.html', messages=RoomList)

@buttonsView.route(f'/scenes', methods=['POST', 'GET'])
def setupScenes():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'buttonsView.home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'buttonsView.home'))
    try: 
        RoomID = request.args['RoomID']
        validRoomID = checkRooms(RoomID)
        if not validRoomID: 
            flash(u'Invalid Room!', 'error')
            return redirect(url_for(f'buttonsView.setupRoom', macAddress=macAddress))
    except: 
        flash(u'Invalid Room!', 'error')
        return redirect(url_for(f'buttonsView.setupRoom', macAddress=macAddress))
    SceneList, SceneCodeList = getScenes(RoomID)
    try:
        if request.method == 'POST': 
            if request.form['buttons'] == 'Cancel':
                flash(u'Set-up cancelled successfully!', 'info')
                return redirect(url_for('buttonsView.home'))
            ScenesSelectedScenes = request.form.getlist('scenes')
            ScenesCode = ""
            if len(ScenesSelectedScenes) < 2:
                flash(u'Please choose at least 2 scenes!', 'error')
                return redirect(url_for(f'buttonsView.setupScenes', macAddress=macAddress, RoomID=RoomID))
            for i in range(len(ScenesSelectedScenes)):
                ScenesCode = ScenesCode + f"{SceneCodeList[ScenesSelectedScenes[i]]},"
            return redirect(url_for(f'buttonsView.setupConfirm', macAddress=macAddress, RoomID=RoomID, SceneCodeList=ScenesCode[:-1]))
    except: 
        flash(u'Please choose at least 2 scenes!', 'error')
        return redirect(url_for(f'buttonsView.setupScenes', macAddress=macAddress, RoomID=RoomID))
    return render_template('setupScenes.html', messages=SceneList)

@buttonsView.route('/confirm', methods=['POST', 'GET'])
def setupConfirm():
    try: 
        macAddress = request.args['macAddress']
        validMacAddress = checkMacAddress(macAddress)
        if not validMacAddress: 
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'buttonsView.home'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'buttonsView.home'))
    try: 
        RoomID = request.args['RoomID']
        validRoomID = checkRooms(RoomID)
        if not validRoomID: 
            flash(u'Invalid Room!', 'error')
            return redirect(url_for(f'buttonsView.setupRoom', macAddress=macAddress))
    except: 
        flash(u'Invalid Room!', 'error')
        return redirect(url_for(f'buttonsView.setupRoom', macAddress=macAddress))
    try: 
        SceneCode = request.args['SceneCodeList']
        SceneCodesList = SceneCode.split(",")
    except: 
        flash(u'Invalid Scene(s)!', 'error')
        return redirect(url_for(f'buttonsView.setupScenes', macAddress=macAddress, RoomID=RoomID))
    SceneNamesList = ScenesIDtoName(SceneCodesList)
    if request.method == 'POST':
        if request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('buttonsView.home'))
        elif request.form['buttons'] == 'Confirm':
            try:
                confirmConfig(FullmacAddress=macAddress, RoomNumber=RoomID, listScene=SceneCodesList, listSceneName=SceneNamesList)
            except:
                flash(u'Invalid Scene(s)!', 'error')
                return redirect(url_for(f'buttonsView.setupScenes', macAddress=macAddress, RoomID=RoomID))
            flash(u'Changes saved successfully!', 'success')
            return redirect(url_for('buttonsView.home'))
    return render_template('setupConfirm.html', messages=SceneNamesList, macAddress=macAddress, SceneNamesList=",".join(list(SceneNamesList.values())))