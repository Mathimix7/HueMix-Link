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

function openSerialModal() {
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

    const select = document.getElementById('serialFirmwareSelect');
    
    // Enable/disable options based on available firmwares
    Array.from(select.options).forEach(opt => {
        if (availableFirmwares[opt.value]) {
            opt.disabled = false;
            const fw = availableFirmwares[opt.value];
            opt.textContent = `${opt.textContent} (v${fw.version})`;
        } else {
            opt.disabled = true;
        }
    });

    // Select first available firmware by default
    const firstAvailable = Array.from(select.options).find(opt => !opt.disabled);
    if (firstAvailable) {
        select.value = firstAvailable.value;
    }

    document.getElementById('serialModal').classList.remove('hidden');
}

async function handleSerialFlash() {
    const firmwareKey = document.getElementById('serialFirmwareSelect').value;
    const fwInfo = availableFirmwares[firmwareKey];
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

        // 3. Prepare flash files (bootloader/partitions for ESP32 only)
        const flashFiles = [];
        const isESP32 = chip.includes('ESP32');
        const isESP8266 = chip.includes('ESP8266');
        
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
        showError("Serial Flash Failed");
    } finally {
        if (transport) await transport.disconnect();
    }
}
