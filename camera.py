import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# --- Configuration ---
CAMERA_IP = "192.168.10.123"
CAMERA_PORT = 8031
LOCAL_PROXY_PORT = 8080

PAYLOAD = bytes.fromhex("999901000000000000000000000000000000000000000000")

latest_frame = b''

def keep_camera_awake_and_receive():
    global latest_frame
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    sock.bind(('0.0.0.0', 0))
    
    last_ping_time = 0
    current_jpeg_buffer = bytearray()
    
    # Based on the packet capture, the proprietary header is 24 bytes long
    HEADER_SIZE = 24 
    
    print(f"[*] Anti-Glitch Streamer Connecting to {CAMERA_IP}:{CAMERA_PORT}...")
    
    while True:
        # 1. Send the keep-alive heartbeat
        if time.time() - last_ping_time > 2.0:
            try:
                sock.sendto(PAYLOAD, (CAMERA_IP, CAMERA_PORT))
                last_ping_time = time.time()
            except:
                pass
        
        try:
            # 2. Receive the video data
            data, addr = sock.recvfrom(65535) 
            if addr[0] == CAMERA_IP:
                
                # Check if this packet contains the START of a new JPEG image
                start_idx = data.find(b'\xff\xd8')
                
                if start_idx != -1:
                    # Before we start a new frame, check if the OLD one finished properly.
                    # A valid JPEG always ends with FF D9.
                    end_idx = current_jpeg_buffer.find(b'\xff\xd9')
                    if end_idx != -1:
                        # We have a perfect, complete frame! Update the live stream.
                        # We slice it exactly at the end marker to remove trailing garbage.
                        latest_frame = bytes(current_jpeg_buffer[:end_idx+2])
                    
                    # Start building the new frame
                    current_jpeg_buffer = bytearray(data[start_idx:])
                    
                else:
                    # This is a continuation packet. 
                    # We must strip the camera's proprietary header so we don't corrupt the image.
                    if len(data) > HEADER_SIZE:
                        # Check if this piece happens to contain the End Of Image marker
                        end_idx = data.find(b'\xff\xd9')
                        if end_idx != -1:
                            # Add the payload up to the end marker
                            current_jpeg_buffer.extend(data[HEADER_SIZE:end_idx+2])
                            # Frame is complete! Update the stream immediately.
                            latest_frame = bytes(current_jpeg_buffer)
                            current_jpeg_buffer = bytearray() # Reset for the next frame
                        else:
                            # Just a middle fragment, append it after stripping the header
                            current_jpeg_buffer.extend(data[HEADER_SIZE:])
                    
        except socket.timeout:
            pass
        except Exception as e:
            pass

class CamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.endswith('.mjpg'):
            self.send_response(200)
            self.send_header('Content-type', 'multipart/x-mixed-replace; boundary=--jpgboundary')
            self.end_headers()
            
            while True:
                try:
                    if latest_frame:
                        self.wfile.write(b"--jpgboundary\r\n")
                        self.send_header('Content-type', 'image/jpeg')
                        self.send_header('Content-length', str(len(latest_frame)))
                        self.end_headers()
                        self.wfile.write(latest_frame)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.05) # Cap at 20 FPS to save browser memory
                except Exception:
                    break
        else:
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            # Simple HTML page with a black background to view the stream
            self.wfile.write(b"<html><head><title>Camera Stream</title></head>")
            self.wfile.write(b"<body style='background-color: black; text-align: center;'>")
            self.wfile.write(b"<img src='/stream.mjpg' style='max-height: 100vh;' /></body></html>")

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    pass

def start_http_server():
    server = ThreadedHTTPServer(('0.0.0.0', LOCAL_PROXY_PORT), CamHandler)
    print(f"[*] Clean stream started. Open your web browser to: http://localhost:{LOCAL_PROXY_PORT}")
    server.serve_forever()

if __name__ == '__main__':
    t = threading.Thread(target=keep_camera_awake_and_receive)
    t.daemon = True
    t.start()
    start_http_server()