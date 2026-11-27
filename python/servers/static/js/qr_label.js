/**
 * Zero-dependency ISO/IEC 18004 QR Code SVG generator (Versions 1-6, EC Level M, Byte Mode)
 * and shared printable HueMix-Link Security v2 PIN label helper.
 */
(function (global) {
    'use strict';

    // GF(256) log/exp tables with primitive polynomial 0x11D
    const GF_EXP = new Uint8Array(512);
    const GF_LOG = new Uint8Array(256);
    (function initGF() {
        let x = 1;
        for (let i = 0; i < 255; i++) {
            GF_EXP[i] = x;
            GF_LOG[x] = i;
            x <<= 1;
            if (x & 0x100) x ^= 0x11d;
        }
        for (let i = 255; i < 512; i++) {
            GF_EXP[i] = GF_EXP[i - 255];
        }
    })();

    function gfMul(a, b) {
        if (a === 0 || b === 0) return 0;
        return GF_EXP[GF_LOG[a] + GF_LOG[b]];
    }

    function rsGeneratorPoly(ecLen) {
        let poly = [1];
        for (let i = 0; i < ecLen; i++) {
            const next = new Array(poly.length + 1).fill(0);
            const factor = GF_EXP[i];
            for (let j = 0; j < poly.length; j++) {
                next[j] ^= poly[j];
                next[j + 1] ^= gfMul(poly[j], factor);
            }
            poly = next;
        }
        return poly;
    }

    function rsEncode(data, ecLen) {
        const gen = rsGeneratorPoly(ecLen);
        const res = new Uint8Array(ecLen);
        for (let i = 0; i < data.length; i++) {
            const factor = data[i] ^ res[0];
            res.copyWithin(0, 1);
            res[ecLen - 1] = 0;
            if (factor !== 0) {
                for (let j = 0; j < ecLen; j++) {
                    res[j] ^= gfMul(gen[j + 1], factor);
                }
            }
        }
        return res;
    }

    // Version specs for EC Level M (versions 1..6, supports up to 106-byte URLs)
    const VERSION_SPECS = [
        null,
        { ver: 1, size: 21, totalData: 16, blocks: 1, ecPerBlock: 10, align: [] },
        { ver: 2, size: 25, totalData: 28, blocks: 1, ecPerBlock: 16, align: [6, 18] },
        { ver: 3, size: 29, totalData: 44, blocks: 1, ecPerBlock: 26, align: [6, 22] },
        { ver: 4, size: 33, totalData: 64, blocks: 2, ecPerBlock: 18, align: [6, 26] },
        { ver: 5, size: 37, totalData: 86, blocks: 2, ecPerBlock: 24, align: [6, 30] },
        { ver: 6, size: 41, totalData: 108, blocks: 4, ecPerBlock: 16, align: [6, 34] },
    ];

    function buildCodewords(bytes, spec) {
        const bits = [];
        const pushBits = (val, len) => {
            for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1);
        };
        // Byte mode (0100) + 8-bit length + bytes + terminator
        pushBits(0b0100, 4);
        pushBits(bytes.length, 8);
        for (let i = 0; i < bytes.length; i++) pushBits(bytes[i], 8);

        const maxBits = spec.totalData * 8;
        const term = Math.min(4, maxBits - bits.length);
        for (let i = 0; i < term; i++) bits.push(0);
        while (bits.length % 8 !== 0) bits.push(0);

        const dataCW = [];
        for (let i = 0; i < bits.length; i += 8) {
            let b = 0;
            for (let j = 0; j < 8; j++) b = (b << 1) | bits[i + j];
            dataCW.push(b);
        }
        const padBytes = [0xec, 0x11];
        let padIdx = 0;
        while (dataCW.length < spec.totalData) {
            dataCW.push(padBytes[padIdx++ & 1]);
        }

        // Split into equal blocks (versions 1..6 EC-M all divide evenly)
        const blockLen = spec.totalData / spec.blocks;
        const dataBlocks = [];
        const ecBlocks = [];
        for (let b = 0; b < spec.blocks; b++) {
            const slice = dataCW.slice(b * blockLen, (b + 1) * blockLen);
            dataBlocks.push(slice);
            ecBlocks.push(rsEncode(slice, spec.ecPerBlock));
        }

        const out = [];
        for (let i = 0; i < blockLen; i++) {
            for (let b = 0; b < spec.blocks; b++) out.push(dataBlocks[b][i]);
        }
        for (let i = 0; i < spec.ecPerBlock; i++) {
            for (let b = 0; b < spec.blocks; b++) out.push(ecBlocks[b][i]);
        }
        return out;
    }

    function formatInfoBits(mask) {
        // EC Level M = 00
        const data = (0b00 << 3) | mask;
        let rem = data << 10;
        for (let i = 14; i >= 10; i--) {
            if ((rem >> i) & 1) rem ^= 0x537 << (i - 10);
        }
        return ((data << 10) | rem) ^ 0x5412;
    }

    function applyMaskBit(mask, r, c) {
        switch (mask) {
            case 0: return (r + c) % 2 === 0;
            case 1: return r % 2 === 0;
            case 2: return c % 3 === 0;
            case 3: return (r + c) % 3 === 0;
            case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
            case 5: return ((r * c) % 2 + (r * c) % 3) === 0;
            case 6: return (((r * c) % 2 + (r * c) % 3) % 2) === 0;
            case 7: return (((r + c) % 2 + (r * c) % 3) % 2) === 0;
            default: return false;
        }
    }

    function penaltyScore(mat, size) {
        let score = 0;
        // Rule 1: runs of 5+ same color
        for (let r = 0; r < size; r++) {
            let run = 1;
            for (let c = 1; c < size; c++) {
                if (mat[r][c] === mat[r][c - 1]) {
                    run++;
                    if (run === 5) score += 3;
                    else if (run > 5) score += 1;
                } else {
                    run = 1;
                }
            }
        }
        for (let c = 0; c < size; c++) {
            let run = 1;
            for (let r = 1; r < size; r++) {
                if (mat[r][c] === mat[r - 1][c]) {
                    run++;
                    if (run === 5) score += 3;
                    else if (run > 5) score += 1;
                } else {
                    run = 1;
                }
            }
        }
        // Rule 2: 2x2 blocks
        for (let r = 0; r < size - 1; r++) {
            for (let c = 0; c < size - 1; c++) {
                const v = mat[r][c];
                if (v === mat[r][c + 1] && v === mat[r + 1][c] && v === mat[r + 1][c + 1]) {
                    score += 3;
                }
            }
        }
        return score;
    }

    function generateQrMatrix(text) {
        const bytes = new TextEncoder().encode(text);
        let spec = null;
        for (let v = 1; v < VERSION_SPECS.length; v++) {
            // 4-bit mode + 8-bit count + bytes <= totalData * 8 -> bytes.length <= totalData - 2
            if (bytes.length <= VERSION_SPECS[v].totalData - 2) {
                spec = VERSION_SPECS[v];
                break;
            }
        }
        if (!spec) {
            throw new Error(`QR payload too long (${bytes.length} bytes)`);
        }

        const size = spec.size;
        const codewords = buildCodewords(bytes, spec);
        const base = Array.from({ length: size }, () => new Int8Array(size).fill(-1));
        const isFunc = Array.from({ length: size }, () => new Uint8Array(size));

        const setFunc = (r, c, val) => {
            if (r < 0 || r >= size || c < 0 || c >= size) return;
            base[r][c] = val ? 1 : 0;
            isFunc[r][c] = 1;
        };

        // Finder patterns + separators
        const drawFinder = (tr, tc) => {
            for (let dr = -1; dr <= 7; dr++) {
                for (let dc = -1; dc <= 7; dc++) {
                    const r = tr + dr;
                    const c = tc + dc;
                    if (r < 0 || r >= size || c < 0 || c >= size) continue;
                    const inOuter = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6;
                    const dark = inOuter && (
                        dr === 0 || dr === 6 || dc === 0 || dc === 6 ||
                        (dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4)
                    );
                    setFunc(r, c, dark);
                }
            }
        };
        drawFinder(0, 0);
        drawFinder(0, size - 7);
        drawFinder(size - 7, 0);

        // Timing patterns
        for (let i = 8; i < size - 8; i++) {
            setFunc(6, i, i % 2 === 0);
            setFunc(i, 6, i % 2 === 0);
        }

        // Alignment patterns
        for (const r of spec.align) {
            for (const c of spec.align) {
                if (isFunc[r][c]) continue;
                for (let dr = -2; dr <= 2; dr++) {
                    for (let dc = -2; dc <= 2; dc++) {
                        const dark = Math.max(Math.abs(dr), Math.abs(dc)) !== 1;
                        setFunc(r + dr, c + dc, dark);
                    }
                }
            }
        }

        // Reserve format info strips + dark module
        for (let i = 0; i <= 8; i++) {
            if (!isFunc[8][i]) setFunc(8, i, 0);
            if (!isFunc[i][8]) setFunc(i, 8, 0);
        }
        for (let i = 0; i < 8; i++) {
            setFunc(8, size - 1 - i, 0);
            setFunc(size - 1 - i, 8, 0);
        }
        setFunc(4 * spec.ver + 9, 8, 1);

        // Place data bits in zigzag
        let bitIdx = 0;
        const totalBits = codewords.length * 8;
        let upward = true;
        for (let col = size - 1; col >= 1; col -= 2) {
            if (col === 6) col = 5;
            for (let step = 0; step < size; step++) {
                const r = upward ? (size - 1 - step) : step;
                for (let dx = 0; dx < 2; dx++) {
                    const c = col - dx;
                    if (isFunc[r][c]) continue;
                    let bit = 0;
                    if (bitIdx < totalBits) {
                        bit = (codewords[bitIdx >> 3] >> (7 - (bitIdx & 7))) & 1;
                        bitIdx++;
                    }
                    base[r][c] = bit;
                }
            }
            upward = !upward;
        }

        // Evaluate all 8 masks and pick the lowest penalty
        let bestMat = null;
        let bestScore = Infinity;
        for (let mask = 0; mask < 8; mask++) {
            const mat = base.map((row, r) => {
                const copy = new Uint8Array(size);
                for (let c = 0; c < size; c++) {
                    copy[c] = isFunc[r][c] ? row[c] : (row[c] ^ (applyMaskBit(mask, r, c) ? 1 : 0));
                }
                return copy;
            });

            // Write format info
            const fmt = formatInfoBits(mask);
            const fmtBit = (i) => (fmt >> i) & 1;
            // Around top-left finder
            for (let i = 0; i <= 5; i++) mat[8][i] = fmtBit(14 - i);
            mat[8][7] = fmtBit(8);
            mat[8][8] = fmtBit(7);
            mat[7][8] = fmtBit(6);
            for (let i = 0; i <= 5; i++) mat[5 - i][8] = fmtBit(5 - i);
            // Bottom-left (col 8, bottom-to-top: bits 14..8) & top-right (row 8, left-to-right: bits 7..0)
            for (let i = 0; i <= 6; i++) mat[size - 1 - i][8] = fmtBit(14 - i);
            for (let i = 0; i <= 7; i++) mat[8][size - 8 + i] = fmtBit(7 - i);

            const score = penaltyScore(mat, size);
            if (score < bestScore) {
                bestScore = score;
                bestMat = mat;
            }
        }
        return bestMat;
    }

    function generateQrSvg(text, pxSize) {
        const mat = generateQrMatrix(text);
        const n = mat.length;
        const quiet = 4;
        const total = n + quiet * 2;
        let path = '';
        for (let r = 0; r < n; r++) {
            for (let c = 0; c < n; c++) {
                if (mat[r][c]) {
                    path += `M${c + quiet},${r + quiet}h1v1h-1z`;
                }
            }
        }
        return `<svg xmlns="http://www.w3.org/2000/svg" width="${pxSize}" height="${pxSize}" viewBox="0 0 ${total} ${total}" shape-rendering="crispEdges"><rect width="${total}" height="${total}" fill="#ffffff"/><path d="${path}" fill="#000000"/></svg>`;
    }

    /**
     * Open a consistent, printable HueMix-Link pairing label containing:
     *  - "HueMix-Link" title
     *  - QR code linking to /pairing#pin=<6-digit-pin>
     *  - 6-digit PIN written below (XXX XXX)
     */
    function printHueMixPinLabel(pin) {
        const cleanPin = String(pin || '').trim();
        if (!/^\d{6}$/.test(cleanPin)) return false;

        const pairingUrl = `${window.location.origin}/pairing#pin=${cleanPin}`;
        const qrSvg = generateQrSvg(pairingUrl, 160);
        const formattedPin = `${cleanPin.slice(0, 3)} ${cleanPin.slice(3)}`;

        const win = window.open('', '_blank', 'width=340,height=380');
        if (!win) {
            if (typeof global.showError === 'function') {
                global.showError('Pop-up blocked; please allow pop-ups to print the label');
            }
            return false;
        }

        win.document.write(`<!DOCTYPE html>
<html>
<head>
    <title>HueMix-Link Pairing Label</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 16px;
            background: #fff;
            color: #000;
        }
        .label {
            border: 2px solid #000;
            border-radius: 10px;
            padding: 14px 18px;
            text-align: center;
            display: inline-flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
        }
        .brand {
            font-size: 16px;
            font-weight: 700;
            letter-spacing: 0.3px;
        }
        .qr {
            line-height: 0;
        }
        .pin {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 24px;
            font-weight: 700;
            letter-spacing: 4px;
        }
        @media print {
            body { min-height: auto; padding: 0; }
        }
    </style>
</head>
<body>
    <div class="label">
        <div class="brand">HueMix-Link</div>
        <div class="qr">${qrSvg}</div>
        <div class="pin">${formattedPin}</div>
    </div>
    <script>window.onload = function() { window.print(); };<\/script>
</body>
</html>`);
        win.document.close();
        return true;
    }

    global.HueMixQR = { generateQrMatrix, generateQrSvg };
    global.printHueMixPinLabel = printHueMixPinLabel;
})(window);
