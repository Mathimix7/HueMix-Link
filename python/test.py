import socket
import struct
import binascii
import time
import random

# --- CONFIGURATION ---
UDP_IP = "0.0.0.0"
UDP_PORT = 7777
MY_HOME_ID = 0xCAFEBABE 
GATEWAY_PORT = 4210

# --- PROTOCOL CONSTANTS ---
PKT_PAIR_CONFIRM = 0x01
PKT_LIGHT_RAW    = 0x02
PKT_SYS_CMD      = 0x04
PKT_GW_LIST_UPD  = 0x05

PKT_HELLO        = 0x10
PKT_BTN_EVENT    = 0x11
PKT_SCENE_REQ    = 0x12
PKT_DELIVERY_RPT = 0x13

# --- ACTION CODES ---
ACT_CLICK   = 1
ACT_HOLDING = 2
ACT_RELEASE = 3
ACT_SYNC    = 9

# --- GLOBAL STATE ---
# known_gateways = { "RADIO_MAC": "IP_ADDR" }
known_gateways = {}
# known_lights = { "LIGHT_MAC": { "gateway_ip": "...", "leds": 60, "rgbw": True } }
known_lights = {}

# --- HELPER: HASHING ---
def calculate_hash(payload_bytes, home_id):
    hash_val = 2166136261
    for b in struct.pack("<I", home_id):
        hash_val ^= b
        hash_val = (hash_val * 16777619) & 0xFFFFFFFF
    for b in payload_bytes:
        hash_val ^= b
        hash_val = (hash_val * 16777619) & 0xFFFFFFFF
    return hash_val

# --- HELPER: FORMAT MAC ---
def format_mac(mac_bytes):
    return binascii.hexlify(mac_bytes, ':').decode('utf-8').upper()

# --- HELPER: SEND PAIRING CONFIG ---
def send_config(sock, ip, port, new_home_id, target_mac_bytes):
    print(f"⚙️  Sending Configuration to {ip}:{port}...")
    payload = struct.pack("<IB", new_home_id, 0)
    signature = calculate_hash(payload, 0)
    src_mac = b'\x00'*6
    header = struct.pack("<BI6s6sB", PKT_PAIR_CONFIRM, signature, src_mac, target_mac_bytes, 0)
    sock.sendto(header + payload, (ip, port))
    print("   -> Config Packet Sent.")

# --- HELPER: SEND LIGHT COLOR ---
def send_light_color(sock, target_light_mac_str, r, g, b, brightness=255):
    if target_light_mac_str not in known_lights:
        print(f"❌ Cannot control light {target_light_mac_str} (Unknown / Offline)")
        return

    light_info = known_lights[target_light_mac_str]
    gateway_ip = light_info['gateway_ip']
    num_leds = light_info['leds']

    # Convert Target MAC to Bytes
    tgt_mac = binascii.unhexlify(target_light_mac_str.replace(":", ""))
    
    # Payload: [Count(1), Brightness(1), Data...]
    payload = struct.pack("BB", num_leds, brightness)
    
    # Generate Color Data (3 bytes per pixel always, FastLED handles conversion)
    # Wait, our protocol is strictly 3 bytes per pixel?
    # Let's check Light Firmware logic:
    # "uint8_t* d = rx->payload.light.data;"
    # "leds[i] = CRGB(d[idx], d[idx+1], d[idx+2]); idx += 3;"
    # YES, the protocol expects RGB stream.
    
    color_data = bytes([r, g, b]) * num_leds
    payload += color_data
    
    # Pad to match struct size (182 bytes total: 2 header + 180 data)
    if len(color_data) < 180:
        payload += b'\x00' * (180 - len(color_data))
        
    # Hash
    signature = calculate_hash(payload, MY_HOME_ID)
    
    # Header
    src_mac = b'\x00'*6
    header = struct.pack("<BI6s6sB", PKT_LIGHT_RAW, signature, src_mac, tgt_mac, 0)
    
    sock.sendto(header + payload, (gateway_ip, GATEWAY_PORT))
    print(f"🎨 Sent Color to {target_light_mac_str} on {gateway_ip}:{GATEWAY_PORT}")

# --- HELPER: PUSH GATEWAY LIST ---
def push_gateway_list(sock):
    if not known_gateways: return
    print(f"🌍 Broadcasting Gateway List...")
    
    count = len(known_gateways)
    if count > 10: count = 10
    
    payload = struct.pack("B", count)
    i = 0
    for radio_mac_str in known_gateways:
        if i >= 10: break
        mac_bytes = binascii.unhexlify(radio_mac_str.replace(":", ""))
        payload += mac_bytes
        i += 1
        
    padding = 61 - len(payload)
    if padding > 0: payload += b'\x00' * padding

    signature = calculate_hash(payload, MY_HOME_ID)
    src_mac = b'\x00'*6
    tgt_mac = b'\xFF'*6 
    header = struct.pack("<BI6s6sB", PKT_GW_LIST_UPD, signature, src_mac, tgt_mac, 0)
    
    packet = header + payload

    for radio_mac, ip in known_gateways.items():
        try: sock.sendto(packet, (ip, UDP_PORT))
        except: pass

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    
    print(f"========================================")
    print(f"   HUEMIX SERVER v2 | Port: {UDP_PORT}")
    print(f"   HomeID: {hex(MY_HOME_ID)}")
    print(f"========================================")

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            sender_ip, sender_port = addr
            if len(data) < 18: continue

            # Header
            header_fmt = "<BI6s6sB"
            h_size = struct.calcsize(header_fmt)
            pkt_type, signature, src_mac_bytes, tgt_mac_bytes, msg_id = struct.unpack(header_fmt, data[:h_size])
            
            src_mac = format_mac(src_mac_bytes)
            raw_payload = data[h_size:]

            # Payload Hashing Selection
            payload_to_hash = b""
            if pkt_type == PKT_HELLO:
                dev_type = raw_payload[0]
                if dev_type == 1: payload_to_hash = raw_payload[:7] 
                elif dev_type == 2: payload_to_hash = raw_payload[:1]
                elif dev_type == 3: 
                    # Restore byte 1 to 0 for hash check (RSSI masking)
                    temp = bytearray(raw_payload[:5])
                    temp[1] = 0
                    payload_to_hash = bytes(temp)

            elif pkt_type == PKT_BTN_EVENT: payload_to_hash = raw_payload[:3]
            elif pkt_type == PKT_DELIVERY_RPT: payload_to_hash = raw_payload[:8]
            else: payload_to_hash = raw_payload 

            sig_zero = calculate_hash(payload_to_hash, 0)
            sig_valid = calculate_hash(payload_to_hash, MY_HOME_ID)

            # --- UNPAIRED LOGIC ---
            if signature == sig_zero:
                print(f"⚠️  NEW DEVICE DETECTED: {src_mac} (IP: {sender_ip})")
                
                if pkt_type == PKT_HELLO:
                    dev_type = raw_payload[0]
                    
                    if dev_type == 1: # Gateway
                        radio_mac = format_mac(raw_payload[1:7])
                        print(f"   -> GATEWAY: {radio_mac}")
                        send_config(sock, sender_ip, sender_port, MY_HOME_ID, src_mac_bytes)
                        known_gateways[radio_mac] = sender_ip
                        time.sleep(0.1); push_gateway_list(sock)

                    elif dev_type == 2: # Button
                        rssi = raw_payload[1] - 256 if raw_payload[1] > 127 else raw_payload[1]
                        if rssi > -50:
                            print(f"   -> BUTTON (RSSI {rssi}). Pairing...")
                            send_config(sock, sender_ip, sender_port, MY_HOME_ID, src_mac_bytes)
                    
                    elif dev_type == 3: # Light Strip
                        rssi = raw_payload[1] - 256 if raw_payload[1] > 127 else raw_payload[1]
                        # Parse Config
                        is_rgbw = (raw_payload[2] == 1)
                        num_leds = (raw_payload[3] << 8) | raw_payload[4]
                        
                        print(f"   -> LIGHT STRIP (RSSI {rssi}). {num_leds} LEDs.")
                        
                        if rssi > -50: # Allow lights to be further
                             print("   -> Pairing Light...")
                             send_config(sock, sender_ip, sender_port, MY_HOME_ID, src_mac_bytes)
                             
                             # Save Light Info for later control
                             known_lights[src_mac] = {
                                 "gateway_ip": sender_ip,
                                 "leds": num_leds,
                                 "rgbw": is_rgbw
                             }

            # --- PAIRED LOGIC ---
            elif signature == sig_valid:
                
                if pkt_type == PKT_HELLO:
                    dev_type = raw_payload[0]
                    if dev_type == 1: 
                        radio_mac = format_mac(raw_payload[1:7])
                        print(f"🌐 GATEWAY ONLINE | WiFi: {src_mac} | Radio: {radio_mac}")
                        known_gateways[radio_mac] = sender_ip
                        push_gateway_list(sock)
                    elif dev_type == 3:
                        # Light re-announcing presence
                        # Update routing table to this gateway IP
                        if src_mac in known_lights:
                            known_lights[src_mac]['gateway_ip'] = sender_ip
                        else:
                            # Default assumption if DB lost
                            known_lights[src_mac] = { "gateway_ip": sender_ip, "leds": 60, "rgbw": True }

                elif pkt_type == PKT_BTN_EVENT:
                    action = raw_payload[0]
                    act_str = {1:"CLICK", 2:"HOLD", 3:"RELEASE", 9:"SYNC_REQ"}.get(action, "UNKNOWN")
                    print(f"🔘 BUTTON {src_mac} -> {act_str}")
                    
                    if action == ACT_CLICK:
                        # --- DEMO: TOGGLE LIGHTS ---
                        # Cycle colors Red -> Green -> Blue -> Off
                        # You would use a real variable here
                        r = random.randint(0, 255)
                        g = random.randint(0, 255)
                        b = random.randint(0, 255)
                        
                        print(f"   -> Triggering Random Color: {r},{g},{b}")
                        
                        for light_mac in known_lights:
                             send_light_color(sock, light_mac, r, g, b) 

                elif pkt_type == PKT_DELIVERY_RPT:
                    success = raw_payload[1]
                    tgt = format_mac(raw_payload[2:8])
                    status = "✅ Delivered" if success else "❌ Failed"
                    print(f"📡 REPORT: Msg to {tgt} -> {status}")

            else:
                if src_mac != "00:00:00:00:00:00":
                    print(f"❌ [{src_mac}] INVALID SIGNATURE")

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    main()