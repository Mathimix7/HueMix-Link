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

    try {
        // 1. Request Browser Serial Port
        const device = await navigator.serial.requestPort();
        logToTerminal("Connecting to device...");

        // 2. Initialize esptool-js transport
        transport = new Transport(device);

        espLoader = new ESPLoader({
            transport: transport,
            baudrate: baudRate,
            terminal: loaderTerminal
        });

        hardReset = new HardReset(transport);

        // Connect/Sync
        const chip = await espLoader.main();
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
        hardReset.reset();
        
        showSuccess("Device flashed successfully via Serial");

    } catch (err) {
        console.error(err);
        logToTerminal(`Error: ${err.message}`);
        showError(err.message ? `Serial Flash Failed: ${err.message}` : "Serial Flash Failed");
    } finally {
        if (transport) await transport.disconnect();
    }
}
