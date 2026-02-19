#!/usr/bin/env python3
import sys
sys.path.insert(0, '/usr/lib/aarch64-linux-gnu/python3.12/site-packages')

from flask import Flask, Response, render_template_string
from hailo_platform import VDevice, HEF, ConfigureParams, InferVStreams, InputVStreamParams, OutputVStreamParams, HailoStreamInterface
import subprocess
import numpy as np
import cv2

app = Flask(__name__)
HTML = '''<!DOCTYPE html><html><head><title>Live Cat Tracker</title>
<style>body{margin:0;background:#000;display:flex;justify-content:center;align-items:center;height:100vh;}
img{max-width:95vw;max-height:95vh;border:3px solid #0f0;}</style></head>
<body><img src="/video"/></body></html>'''

hef, target, network_group = None, None, None
input_vstreams_params, output_vstreams_params, network_group_params = None, None, None
activated_network, infer_pipeline_ctx = None, None

def init_hailo():
    global hef, target, network_group, input_vstreams_params, output_vstreams_params, network_group_params
    hef = HEF("yolov8s.hef")
    target = VDevice()
    configure_params = ConfigureParams.create_from_hef(hef, HailoStreamInterface.PCIe)
    network_group = target.configure(hef, configure_params)[0]
    network_group_params = network_group.create_params()
    input_vstreams_params = InputVStreamParams.make_from_network_group(network_group)
    output_vstreams_params = OutputVStreamParams.make_from_network_group(network_group)
    print("✅ Hailo ready!")

def generate():
    global activated_network, infer_pipeline_ctx
    
    cmd = ["rpicam-vid", "-t", "0", "--codec", "mjpeg", "--width", "640", 
           "--height", "640", "--framerate", "30", "-o", "-", "--nopreview"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    
    input_info = network_group.get_input_vstream_infos()[0]
    
    if activated_network is None:
        activated_network = network_group.activate(network_group_params)
        activated_network.__enter__()
        infer_pipeline_ctx = InferVStreams(network_group, input_vstreams_params, output_vstreams_params)
        infer_pipeline_ctx.__enter__()
    
    buf = b""
    
    try:
        while True:
            buf += proc.stdout.read(4096)
            
            a = buf.find(b'\xff\xd8')
            b = buf.find(b'\xff\xd9')
            
            if a != -1 and b != -1 and b > a:
                jpg = buf[a:b+2]
                buf = buf[b+2:]
                
                # Decode JPEG
                frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                
                # Run Hailo inference
                img_array = np.expand_dims(frame, axis=0)
                output = infer_pipeline_ctx.infer({input_info.name: img_array})
                detections = output['yolov8s/yolov8_nms_postprocess'][0]
                
                # Draw detections
                for class_id, dets in enumerate(detections):
                    for det in dets:
                        x1,y1,x2,y2,conf = det
                        if conf > 0.4:
                            x1_px, y1_px = int(x1*640), int(y1*640)
                            x2_px, y2_px = int(x2*640), int(y2*640)
                            color = (0,255,0) if class_id==15 else (100,100,255)
                            label = "🐱 CAT" if class_id==15 else f"obj"
                            cv2.rectangle(frame, (x1_px,y1_px), (x2_px,y2_px), color, 3)
                            cv2.putText(frame, f"{label} {conf:.0%}", (x1_px,y1_px-10),
                                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                
                # Re-encode with detections
                ret, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpg.tobytes() + b'\r\n')
    finally:
        proc.kill()

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/video")
def video():
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")

if __name__ == '__main__':
    init_hailo()
    print("\n🎉 LIVE Cat Tracker with Hailo AI!")
    print("🌐 http://192.168.1.139:8080\n")
    app.run(host="0.0.0.0", port=8080, threaded=True)
