import { Transport, ESPLoader, HardReset } from "/static/bundle.js";
// Add these to your global variables
let espLoader;
let transport;
let hardReset;
const terminal = document.getElementById('serialTerminal');

// Log helper for the terminal UI
function logToTerminal(message) {
    const p = document.createElement('p');
    p.textContent = `> ${message}`;
    terminal.appendChild(p);
    terminal.scrollTop = terminal.scrollHeight;
}

const loaderTerminal = {
    clean: () => { terminal.innerHTML = ''; },
    writeLine: (data) => { logToTerminal(data); },
    write: (data) => { logToTerminal(data); },
    setup: () => {}
};

// Event Listeners
document.getElementById('serialFlashBtn').addEventListener('click', openSerialModal);
document.getElementById('closeSerialModal').addEventListener('click', () => {
    document.getElementById('serialModal').classList.add('hidden');
});
document.getElementById('connectSerialBtn').addEventListener('click', handleSerialFlash);
document.getElementById('stopSerialBtn').addEventListener('click', handleStopSerialFlash);

let serialAborted = false;
let lastSerialPin = '';
let lastSerialPinMac = '';
let lastSerialPinLabel = '';

function isV2PinFirmwareVersion(versionStr) {
    if (!versionStr) return false;
    const match = String(versionStr).trim().match(/^(\d+)\.(\d+)\.(\d+)/);
    if (!match) return false;
    const major = parseInt(match[1], 10);
    return major >= 4 && major < 5;
}

function hideSerialPinBanner() {
    lastSerialPin = '';
    lastSerialPinMac = '';
    lastSerialPinLabel = '';
    const banner = document.getElementById('serialPinBanner');
    if (banner) banner.classList.add('hidden');
}

function showSerialPinBanner(pin, mac, label) {
    lastSerialPin = pin;
    lastSerialPinMac = mac;
    lastSerialPinLabel = label || 'HueMix-Link Device';
    const banner = document.getElementById('serialPinBanner');
    const infoEl = document.getElementById('serialPinDeviceInfo');
    const valEl = document.getElementById('serialPinValue');
    if (infoEl) infoEl.textContent = `${lastSerialPinLabel} • ${mac}`;
    if (valEl) valEl.textContent = `${pin.slice(0, 3)} ${pin.slice(3)}`;
    if (banner) banner.classList.remove('hidden');
}

document.getElementById('copySerialPinBtn')?.addEventListener('click', () => {
    if (!lastSerialPin) return;
    navigator.clipboard.writeText(lastSerialPin).then(() => showSuccess('PIN copied to clipboard'));
});

document.getElementById('copySerialNfcUrlBtn')?.addEventListener('click', () => {
    if (!lastSerialPin) return;
    const url = `${window.location.origin}/pairing#pin=${lastSerialPin}`;
    navigator.clipboard.writeText(url).then(() => showSuccess('NFC pairing URL copied to clipboard'));
});

document.getElementById('printSerialPinBtn')?.addEventListener('click', () => {
    if (!lastSerialPin) return;
    window.printHueMixPinLabel(lastSerialPin);
});

async function provisionEsp8266PinOverSerial(port, pin) {
    try {
        await port.open({ baudRate: 115200 });
        // Pulse RTS with DTR=false so ESP8266 boots cleanly into app mode (not bootloader)
        try {
            await port.setSignals({ dataTerminalReady: false, requestToSend: true });
            await new Promise(r => setTimeout(r, 100));
            await port.setSignals({ dataTerminalReady: false, requestToSend: false });
        } catch (_) {}

        const encoder = new TextEncoder();
        const decoder = new TextDecoder();
        const writer = port.writable.getWriter();
        const reader = port.readable.getReader();
        let rxBuf = '';
        let acked = false;
        const deadline = Date.now() + 7000;
        let lastSend = 0;

        // Read loop with periodic HML2_PIN:<pin>\n sends until HML2_PIN_OK
        const readPromise = (async () => {
            while (!acked && Date.now() < deadline) {
                const { value, done } = await reader.read();
                if (done) break;
                if (value) {
                    rxBuf += decoder.decode(value, { stream: true });
                    if (rxBuf.includes('HML2_PIN_OK')) {
                        acked = true;
                        break;
                    }
                }
            }
        })();

        while (!acked && Date.now() < deadline) {
            if (Date.now() - lastSend >= 350) {
                lastSend = Date.now();
                await writer.write(encoder.encode(`HML2_PIN:${pin}\n`));
            }
            await new Promise(r => setTimeout(r, 100));
        }

        try { await reader.cancel(); } catch (_) {}
        try { reader.releaseLock(); } catch (_) {}
        try { writer.releaseLock(); } catch (_) {}
        await readPromise.catch(() => {});
        try { await port.close(); } catch (_) {}
        return acked;
    } catch (err) {
        try { await port.close(); } catch (_) {}
        console.warn('ESP8266 serial PIN provisioning error:', err);
        return false;
    }
}

async function handleStopSerialFlash() {
    serialAborted = true;
    logToTerminal("Stopping serial session...");
    if (transport) {
        try {
            await transport.disconnect();
        } catch (err) {
            console.warn("Error while stopping serial transport:", err);
        }
        transport = null;
    }
    document.getElementById('stopSerialBtn').classList.add('hidden');
    document.getElementById('connectSerialBtn').classList.remove('hidden');
    logToTerminal("Stopped. You can reconnect or try 115200 baud if the port stalled.");
}

// Firmware list backing the serial flash modal (keyed by firmware type key)
let serialFirmwares = [];
let serialFirmwareByKey = {};

async function loadSerialFirmwares() {
    try {
        const response = await fetch('/api/ota/serial_firmwares');
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Request failed');
        serialFirmwares = data.firmwares || [];
        serialFirmwareByKey = {};
        serialFirmwares.forEach(fw => { serialFirmwareByKey[fw.key] = fw; });
        return true;
    } catch (err) {
        console.error('Failed to load serial firmwares:', err);
        showError('Failed to load available firmware list');
        serialFirmwares = [];
        serialFirmwareByKey = {};
        return false;
    }
}

function rebuildSerialFirmwareOptions(loaded = true) {
    const select = document.getElementById('serialFirmwareSelect');
    select.innerHTML = '';

    if (!serialFirmwares.length) {
        const empty = document.createElement('option');
        empty.value = '';
        empty.disabled = true;
        empty.textContent = loaded ? 'No firmware available' : 'Failed to load firmware list';
        select.appendChild(empty);
        return null;
    }

    // Group entries preserving server order (built-in groups first, then plugins)
    const groups = new Map();
    serialFirmwares.forEach(fw => {
        const group = fw.group || 'Other';
        if (!groups.has(group)) groups.set(group, []);
        groups.get(group).push(fw);
    });

    groups.forEach((entries, groupName) => {
        const optgroup = document.createElement('optgroup');
        optgroup.label = groupName;
        entries.forEach(fw => {
            const option = document.createElement('option');
            option.value = fw.key;
            option.textContent = `${fw.label} (v${fw.version})`;
            optgroup.appendChild(option);
        });
        select.appendChild(optgroup);
    });

    return serialFirmwares[0];
}

async function openSerialModal() {
    // Check if Web Serial API is available
    if (!navigator.serial) {
        // Check if we're on HTTP and can redirect to HTTPS
        if (window.location.protocol === 'http:') {
            // Replace http:// with https:// and remove any port number
            const httpsUrl = window.location.href
                .replace('http://', 'https://')
                .replace(/:\d+/, '');  // Remove any port number
            showError(`Web Serial API requires HTTPS. <a href="${httpsUrl}" class="underline text-red-700 hover:text-red-800 font-semibold">Click here to switch to HTTPS</a>`);
        } else {
            showError("Web Serial API is not available. Please use a supported browser (Chrome, Edge, etc).");
        }
        return;
    }

    // Rebuild the firmware list from the server so plugin firmwares are included
    hideSerialPinBanner();
    const loaded = await loadSerialFirmwares();
    const firstAvailable = rebuildSerialFirmwareOptions(loaded);
    if (firstAvailable) {
        document.getElementById('serialFirmwareSelect').value = firstAvailable.key;
    }

    document.getElementById('serialModal').classList.remove('hidden');
}

async function handleSerialFlash() {
    const firmwareKey = document.getElementById('serialFirmwareSelect').value;
    const fwInfo = serialFirmwareByKey[firmwareKey];
    const baudRate = parseInt(document.getElementById('serialBaudRate').value);

    if (!fwInfo) return showError("Select a firmware first");

    // Check if Web Serial API is available
    if (!navigator.serial) {
        showError("Web Serial API is not available.");
        return;
    }

    serialAborted = false;
    hideSerialPinBanner();
    const connectBtn = document.getElementById('connectSerialBtn');
    const stopBtn = document.getElementById('stopSerialBtn');
    let provisionedPin = null;
    let provisionedMac = null;
    let esp8266NeedsSerialPin = false;
    let serialDevicePort = null;

    try {
        // 1. Request Browser Serial Port
        const device = await navigator.serial.requestPort();
        serialDevicePort = device;
        connectBtn.classList.add('hidden');
        stopBtn.classList.remove('hidden');
        logToTerminal("Connecting to device...");

        // 2. Initialize esptool-js transport
        transport = new Transport(device);

        // Fix esptool-js bug in bundle.js: Transport.disconnect() calls reader.cancel()
        // without reader.releaseLock(), leaving device.readable.locked === true so
        // waitForUnlock(400) spins forever when changing baud rates (e.g. 460800 / 921600).
        transport.disconnect = async () => {
            if (transport.reader) {
                try {
                    await transport.reader.cancel();
                } catch (_) {}
                try {
                    transport.reader.releaseLock();
                } catch (_) {}
                transport.reader = undefined;
            }
            await transport.waitForUnlock(100);
            if (transport.device && (transport.device.readable || transport.device.writable)) {
                await transport.device.close();
            }
            await new Promise(resolve => setTimeout(resolve, 100));
        };

        espLoader = new ESPLoader({
            transport: transport,
            baudrate: baudRate,
            terminal: loaderTerminal
        });

        hardReset = new HardReset(transport);

        // Connect/Sync
        const chip = await espLoader.main();
        if (serialAborted) return;
        logToTerminal(`Connected to ${chip}`);

        const isESP32 = chip.includes('ESP32');
        const isESP8266 = chip.includes('ESP8266');

        // Abort when the selected firmware targets a different chip family
        if (fwInfo.platform) {
            const connectedPlatform = isESP32 ? 'esp32' : (isESP8266 ? 'esp8266' : null);
            if (connectedPlatform && connectedPlatform !== fwInfo.platform) {
                throw new Error(`Firmware is for ${fwInfo.platform.toUpperCase()}, but the connected device is ${connectedPlatform.toUpperCase()}. Select the matching firmware.`);
            }
        }

        // 3. Prepare flash files (bootloader/partitions for ESP32 only)
        const flashFiles = [];
        
        if (isESP32) {
            logToTerminal("ESP32 detected - Full flash mode (bootloader + partitions + firmware)");
            
            // Fetch bootloader from static folder
            const bootloaderResponse = await fetch('/static/common-binaries/esp32_bootloader.bin');
            if (!bootloaderResponse.ok) throw new Error('Failed to fetch bootloader');
            const bootloaderBuffer = await bootloaderResponse.arrayBuffer();
            const bootloaderArray = new Uint8Array(bootloaderBuffer);
            let bootloaderString = "";
            for (let i = 0; i < bootloaderArray.length; i += 8192) {
                bootloaderString += String.fromCharCode.apply(null, bootloaderArray.subarray(i, i + 8192));
            }
            flashFiles.push({ data: bootloaderString, address: 0x1000 });
            logToTerminal(`  Bootloader: ${(bootloaderBuffer.byteLength / 1024).toFixed(1)} KB @ 0x1000`);
            
            // Fetch partitions from static folder
            const partitionsResponse = await fetch('/static/common-binaries/esp32_partitions.bin');
            if (!partitionsResponse.ok) throw new Error('Failed to fetch partitions');
            const partitionsBuffer = await partitionsResponse.arrayBuffer();
            const partitionsArray = new Uint8Array(partitionsBuffer);
            let partitionsString = "";
            for (let i = 0; i < partitionsArray.length; i += 8192) {
                partitionsString += String.fromCharCode.apply(null, partitionsArray.subarray(i, i + 8192));
            }
            flashFiles.push({ data: partitionsString, address: 0x8000 });
            logToTerminal(`  Partitions: ${(partitionsBuffer.byteLength / 1024).toFixed(1)} KB @ 0x8000`);
        } else if (isESP8266) {
            logToTerminal("ESP8266 detected - Firmware-only flash mode");
        } else {
            throw new Error(`Unsupported chip: ${chip}`);
        }
        
        // Fetch firmware
        logToTerminal("Fetching firmware binary...");
        
        const binaryResponse = await fetch('/api/ota/binary', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source: fwInfo.source,
                download_url: fwInfo.download_url,
                filename: fwInfo.filename,
                filepath: fwInfo.filepath
            })
        });
        
        if (!binaryResponse.ok) {
            const errorData = await binaryResponse.json();
            throw new Error(errorData.error || `HTTP ${binaryResponse.status}: Failed to fetch firmware`);
        }
        
        const firmwareBuffer = await binaryResponse.arrayBuffer();
        if (!firmwareBuffer || firmwareBuffer.byteLength === 0) {
            throw new Error('Firmware file is empty');
        }
        const firmwareArray = new Uint8Array(firmwareBuffer);
        let binaryString = "";
        const chunkSize = 1024 * 8; // Process in 8KB chunks for speed/memory
        for (let i = 0; i < firmwareArray.length; i += chunkSize) {
            binaryString += String.fromCharCode.apply(null, firmwareArray.subarray(i, i + chunkSize));
        }
        
        // Firmware address depends on chip type
        const firmwareAddress = isESP32 ? 0x10000 : 0x00000;
        flashFiles.push({ data: binaryString, address: firmwareAddress });
        logToTerminal(`  Firmware: ${(firmwareBuffer.byteLength / 1024).toFixed(1)} KB @ 0x${firmwareAddress.toString(16)}`);

        // 3b. Security v2 PIN provisioning for v4.x non-gateway firmware (>= 4.0.0 and < 5.0.0)
        // Gateways pair via local WiFi/USB and use TOFU-only Security v2 (no 6-digit PIN).
        const isGatewayFirmware = String(firmwareKey || '').toLowerCase().startsWith('gateway');
        if (isV2PinFirmwareVersion(fwInfo.version) && isGatewayFirmware) {
            logToTerminal('  Gateway firmware uses TOFU Security v2 (no PIN required).');
        } else if (isV2PinFirmwareVersion(fwInfo.version)) {
            let chipMac = '';
            try {
                chipMac = await espLoader.chip.readMac(espLoader);
            } catch (macErr) {
                logToTerminal(`Warning: could not read chip MAC (${macErr.message})`);
            }
            if (chipMac) {
                logToTerminal(`Provisioning Security v2 6-digit PIN for ${chipMac}...`);
                const provResp = await fetch('/api/ota/serial_provision_pin', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        mac: chipMac,
                        version: fwInfo.version,
                        platform: isESP32 ? 'esp32' : 'esp8266',
                        firmware_type: firmwareKey,
                        label: fwInfo.label || firmwareKey
                    })
                });
                const provData = await provResp.json();
                if (!provResp.ok || !provData.success) {
                    throw new Error(provData.error || 'Failed to provision Security v2 PIN');
                }
                provisionedPin = provData.pin;
                provisionedMac = provData.mac;

                if (isESP32 && provData.nvs_b64 && provData.nvs_offset != null) {
                    const nvsString = atob(provData.nvs_b64);
                    flashFiles.push({ data: nvsString, address: provData.nvs_offset });
                    logToTerminal(`  Security v2 PIN NVS: ${(nvsString.length / 1024).toFixed(1)} KB @ 0x${provData.nvs_offset.toString(16)} (PIN: ${provisionedPin.slice(0,3)} ${provisionedPin.slice(3)})`);
                } else if (isESP8266) {
                    esp8266NeedsSerialPin = true;
                    logToTerminal(`  Security v2 PIN ready for post-boot write (PIN: ${provisionedPin.slice(0,3)} ${provisionedPin.slice(3)})`);
                }
            }
        }

        // 4. Flash all files
        logToTerminal(`Starting flash (${flashFiles.length} file${flashFiles.length > 1 ? 's' : ''})...`);
        
        await espLoader.writeFlash({
            fileArray: flashFiles,
            flashSize: 'keep',
            flashMode: isESP32 ? 'dio' : 'qio',  // ESP8266 typically uses QIO
            flashFreq: '40m',
            eraseAll: true,
            compress: true,
            reportProgress: (idx, written, total) => {
                console.log(`Progress: ${Math.round(written/total*100)}%`);
            }
        });

        logToTerminal("FLASH COMPLETE!");
        
        // Hard reset the device to boot into new firmware
        logToTerminal("Resetting device...");
        await hardReset.reset();

        // On ESP8266 (LittleFS Preferences), write the 6-digit PIN over serial right after boot
        if (esp8266NeedsSerialPin && provisionedPin && serialDevicePort) {
            logToTerminal("Writing Security v2 PIN to ESP8266...");
            if (transport) {
                try { await transport.disconnect(); } catch (_) {}
                transport = null;
            }
            await new Promise(r => setTimeout(r, 400));
            const pinAcked = await provisionEsp8266PinOverSerial(serialDevicePort, provisionedPin);
            if (pinAcked) {
                logToTerminal(`✅ ESP8266 confirmed Security v2 PIN (${provisionedPin.slice(0,3)} ${provisionedPin.slice(3)})`);
            } else {
                logToTerminal("⚠️ ESP8266 did not confirm PIN over serial; you can still pair via TOFU and provision the PIN from the OTA page.");
            }
        }

        if (provisionedPin && provisionedMac) {
            showSerialPinBanner(provisionedPin, provisionedMac, fwInfo.label || firmwareKey);
            showSuccess(`Device flashed! Security v2 PIN: ${provisionedPin.slice(0,3)} ${provisionedPin.slice(3)}`);
        } else {
            showSuccess("Device flashed successfully via Serial");
        }

    } catch (err) {
        if (!serialAborted) {
            console.error(err);
            logToTerminal(`Error: ${err.message}`);
            showError(err.message ? `Serial Flash Failed: ${err.message}` : "Serial Flash Failed");
        }
    } finally {
        stopBtn.classList.add('hidden');
        connectBtn.classList.remove('hidden');
        if (transport) {
            try {
                await transport.disconnect();
            } catch (e) {
                console.warn("Cleanup disconnect error:", e);
            }
            transport = null;
        }
    }
}
