from flask import Blueprint, render_template, redirect, url_for, flash, request
from HueConfig import getServers, serversWriteNewName, removeServer, getServerData, time_ago, getServerStatus

serversView = Blueprint('serversView', __name__)

@serversView.route("/", methods=['POST', 'GET'])
def servers():
    try:
        if request.method == 'POST':
            if request.form['buttons'] == 'Delete':
                macAddress = request.form['rooms']
                removeServer(macAddress)
                flash(f'Successfully deleted server {macAddress.split("- ")[0]}!', 'success')
            if request.form['buttons'] == 'Rename':
                macAddress = request.form['rooms']
                return redirect(url_for(f'serversView.rename', macAddress=macAddress))
            if request.form['buttons'] == 'Information':
                macAddress = request.form['rooms']
                return redirect(url_for(f'serversView.info', macAddress=macAddress))
    except:
        flash(u'Please choose the server you want to modify!', 'error')
        return redirect(url_for('serversView.servers'))
    serversJSON = getServers()
    return render_template('servers.html', messages=serversJSON)

@serversView.route("/info", methods=['POST', 'GET'])
def info():
    if request.method == 'POST':
        if request.form['buttons'] == 'Back':
            return redirect(url_for('serversView.servers'))
    macAddress = request.args['macAddress']
    onlyMacAddress = macAddress.split('- ')[1].split(' ')[0]
    serverData = getServerData(onlyMacAddress)
    serverData["lastUsed"] = time_ago(serverData["lastUsed"])
    status = getServerStatus(serverData["ip"])
    serverData.update(status)
    try:
        serverData["led_on_time"] = int(serverData["led_on_time"])
        serverData["led_off_time"] = int(serverData["led_off_time"])
    except: pass
    return render_template('serverinfo.html', data=serverData)

@serversView.route(f'rename', methods = ['POST', 'GET'])
def rename():
    try: 
        macAddress = request.args['macAddress']
        onlyMacAddress = macAddress.split('- ')[1].split(' ')[0]
        macs = []
        data = getServers()
        for device in data:
            macs.append(data[device].split('- ')[1].split(' ')[0])
        if not onlyMacAddress in macs:
            flash(u'Invalid MacAddress!', 'error')
            return redirect(url_for(f'serversView.servers'))
    except: 
        flash(u'Invalid MacAddress!', 'error')
        return redirect(url_for(f'serversView.servers'))
    if request.method == 'POST':
        if request.form['buttons'] == 'Change':
            if len(request.form.get('nname')) != 0:
                if '- ' in request.form.get('nname'):
                    flash(u'Invalid Name!', 'error')
                    return redirect(url_for(f'serversView.rename', macAddress=macAddress))
                serversWriteNewName(macAddress, request.form.get('nname'))
                flash(u'Changes saved successfully!', 'success')
                return redirect(url_for('serversView.servers'))
            else:
                flash(u'Name is too short!', 'error')
                return redirect(url_for(f'serversView.rename', macAddress=macAddress))
        elif request.form['buttons'] == 'Cancel':
            flash(u'Set-up cancelled successfully!', 'info')
            return redirect(url_for('serversView.servers'))
    return render_template('setupRename.html')