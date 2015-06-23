#!/usr/bin/env python
#!coding : utf-8

from __future__ import (
    unicode_literals,
    absolute_import,
    print_function,
    division,
    )
native_str = str
str = type('')

import sys
PY2 = sys.version_info.major == 2
import io
import os
import shutil
from RPIO import PWM
from subprocess import Popen, PIPE
from struct import Struct
from threading import Thread
from time import sleep, time
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
from wsgiref.simple_server import make_server

import picamera
from ws4py.websocket import WebSocket
from ws4py.server.wsgirefserver import WSGIServer, WebSocketWSGIRequestHandler
from ws4py.server.wsgiutils import WebSocketWSGIApplication

import tornado.httpserver
import tornado.websocket
import tornado.ioloop
import tornado.web


WIDTH = 320
HEIGHT = 240
FRAMERATE = 24
HTTP_PORT = 8082
WS_PORT = 8084
JSMPEG_MAGIC = b'jsmp'
JSMPEG_HEADER = Struct(native_str('>4sHH'))
y_axis_value = 500
x_axis_value = 500


class StreamingHttpHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
            return
        elif self.path == '/jsmpg.js':
            content_type = 'application/javascript'
            content = self.server.jsmpg_content
        elif self.path == '/index.html':
            content_type = 'text/html; charset=utf-8'
            content = self.server.index_template.replace(
                    '@ADDRESS@',
                    '%s:%d' % (self.request.getsockname()[0], WS_PORT))
        else:
            self.send_error(404, 'File not found')
            return
        content = content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(content))
        self.send_header('Last-Modified', self.date_time_string(time()))
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(content)


class StreamingHttpServer(HTTPServer):
    def __init__(self):
        # Eurgh ... old-style classes ...
        if PY2:
            HTTPServer.__init__(self, ('', HTTP_PORT), StreamingHttpHandler)
        else:
            super(StreamingHttpServer, self).__init__(
                    ('', HTTP_PORT), StreamingHttpHandler)
        with io.open('index.html', 'r') as f:
            self.index_template = f.read()
        with io.open('jsmpg.js', 'r') as f:
            self.jsmpg_content = f.read()


class StreamingWebSocket(WebSocket):
    def opened(self):
        self.send(JSMPEG_HEADER.pack(JSMPEG_MAGIC, WIDTH, HEIGHT), binary=True)


class BroadcastOutput(object):
    def __init__(self, camera):
        print('Spawning background conversion process')
        self.converter = Popen([
            'avconv',
            '-f', 'rawvideo',
            '-pix_fmt', 'yuv420p',
            '-s', '%dx%d' % camera.resolution,
            '-r', str(float(camera.framerate)),
            '-i', '-',
            '-f', 'mpeg1video',
            '-b', '800k',
            '-r', str(float(camera.framerate)),
            '-'],
            stdin=PIPE, stdout=PIPE, stderr=io.open(os.devnull, 'wb'),
            shell=False, close_fds=True)

    def write(self, b):
        self.converter.stdin.write(b)

    def flush(self):
        print('Waiting for background conversion process to exit')
        self.converter.stdin.close()
        self.converter.wait()


class BroadcastThread(Thread):
    def __init__(self, converter, websocket_server):
        super(BroadcastThread, self).__init__()
        self.converter = converter
        self.websocket_server = websocket_server

    def run(self):
        try:
            while True:
                buf = self.converter.stdout.read(512)
                if buf:
                    self.websocket_server.manager.broadcast(buf, binary=True)
                elif self.converter.poll() is not None:
                    break
        finally:
            self.converter.stdout.close()

class WSHandler(tornado.websocket.WebSocketHandler):
    # global y_axis_value
    # global x_axis_value    

    def check_origin(self, origin):
        return True
    def open(self):
        print ('user is connected.\n')

    def on_message(self, message):
        global y_axis_value
        global x_axis_value
        servo = PWM.Servo()
        print ('received message: %s\n' %message)
        self.write_message(message + ' OK')
        if message == "top":
            if y_axis_value < 700 and y_axis_value > 300:
                y_axis_value = y_axis_value + 10
                print("top ", y_axis_value)
                servo.set_servo(17, int(y_axis_value))
        if message == "right":
            if x_axis_value < 700 and x_axis_value > 300:
                x_axis_value = x_axis_value + 10
                print("right ", x_axis_value)
                servo.set_servo(18, int(x_axis_value))
        if message == "left":
            if x_axis_value < 700 and x_axis_value > 300:
                x_axis_value = x_axis_value - 10
                print("left ", x_axis_value)
                servo.set_servo(18, int(x_axis_value))
        if message == "bottom":
            if y_axis_value < 700 and y_axis_value > 300:
                y_axis_value = y_axis_value - 10
                print("bottom ", y_axis_value)
                servo.set_servo(17, int(y_axis_value))

    def on_close(self):
        print ('connection closed\n')

def ws_servo_thread():
    print ('start servo thread()')
    application = tornado.web.Application([(r'/ws', WSHandler),])
    http_server = tornado.httpserver.HTTPServer(application)
    http_server.listen(3000)
    tornado.ioloop.IOLoop.instance().start()

def main():
    
    print('Initializing camera')
    with picamera.PiCamera() as camera:
        camera.resolution = (WIDTH, HEIGHT)
        camera.framerate = FRAMERATE
        sleep(1) # camera warm-up time
        print('Initializing websockets server on port %d' % WS_PORT)
        websocket_server = make_server(
            '', WS_PORT,
            server_class=WSGIServer,
            handler_class=WebSocketWSGIRequestHandler,
            app=WebSocketWSGIApplication(handler_cls=StreamingWebSocket))
        websocket_server.initialize_websockets_manager()
        websocket_thread = Thread(target=websocket_server.serve_forever)
        print('Initializing HTTP server on port %d' % HTTP_PORT)
        http_server = StreamingHttpServer()
        http_thread = Thread(target=http_server.serve_forever)
        print('Initializing broadcast thread')
        output = BroadcastOutput(camera)
        broadcast_thread = BroadcastThread(output.converter, websocket_server)
        print('Starting recording')
        camera.start_recording(output, 'yuv')
        servo_thread = Thread(target=ws_servo_thread)
        try:
            print('Starting websockets thread')
            websocket_thread.start()
            print('Starting HTTP server thread')
            http_thread.start()
            print('Starting broadcast thread')
            broadcast_thread.start()
            print('Starting servo thread')
            servo_thread.start()       
            while True:
                camera.wait_recording(1)
        except KeyboardInterrupt:
            pass
        finally:
            print('Stopping recording')
            camera.stop_recording()
            print('Waiting for broadcast thread to finish')
            broadcast_thread.join()
            print('Shutting down HTTP server')
            http_server.shutdown()
            print('Shutting down websockets server')
            websocket_server.shutdown()
            print('Waiting for HTTP server thread to finish')
            http_thread.join()
            print('Waiting for websockets thread to finish')
            websocket_thread.join()
            print('Waiting for servo thread to finish')
            servo_thread.join()


if __name__ == '__main__':
    main()