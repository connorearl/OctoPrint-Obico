import io
import re
import os
import logging
import subprocess
import time
import sarge
import sys
import flask
from collections import deque
try:
    import queue
except ImportError:
    import Queue as queue
try:
    ModuleNotFoundError
except NameError:
    ModuleNotFoundError = ImportError
from threading import Thread, RLock
import requests
import backoff
import json
import socket
import errno
import base64
from textwrap import wrap
import psutil
from octoprint.util import to_unicode

from .janus import JANUS_SERVER
from .utils import pi_version, ExpoBackoff, get_image_info, wait_for_port, wait_for_port_to_close
from .lib import alert_queue
from .webcam_capture import capture_jpeg, webcam_full_url

_logger = logging.getLogger('octoprint.plugins.obico')

FFMPEG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin', 'ffmpeg')

PI_CAM_RESOLUTIONS = {
    'low': ((320, 240), (480, 270)),  # resolution for 4:3 and 16:9
    'medium': ((640, 480), (960, 540)),
    'high': ((1296, 972), (1640, 922)),
    'ultra_high': ((1640, 1232), (1920, 1080)),
}


def bitrate_for_dim(img_w, img_h):
    dim = img_w * img_h
    if dim <= 480 * 270:
        return 400*1000
    if dim <= 960 * 540:
        return 1300*1000
    if dim <= 1280 * 720:
        return 2000*1000
    else:
        return 3000*1000


def cpu_watch_dog(watched_process, plugin, max, interval):

    def watch_process_cpu(watched_process, max, interval, plugin):
        while True:
            if not watched_process.is_running():
                return

            cpu_pct = watched_process.cpu_percent(interval=None)
            if cpu_pct > max:
                alert_queue.add_alert({'level': 'warning', 'cause': 'cpu'}, plugin)

            time.sleep(interval)

    watch_thread = Thread(target=watch_process_cpu, args=(watched_process, max, interval, plugin))
    watch_thread.daemon = True
    watch_thread.start()


def is_octolapse_enabled(plugin):
    octolapse_plugin = plugin._plugin_manager.get_plugin_info('octolapse', True)
    if octolapse_plugin is None:
        # not installed or not enabled
        return False

    return octolapse_plugin.implementation._octolapse_settings.main_settings.is_octolapse_enabled


class WebcamStreamer:

    def __init__(self, plugin, sentry):
        self.plugin = plugin
        self.sentry = sentry

        self.pi_camera = None
        self.webcam_server = None
        self.ffmpeg_proc = None
        self.shutting_down = False
        self.compat_streaming = False

    @backoff.on_exception(backoff.expo, Exception, max_tries=5)
    def __init_camera__(self):
        try:
            import picamera
            try:
                self.pi_camera = picamera.PiCamera()
                self.pi_camera.framerate = 20
                (res_43, res_169) = PI_CAM_RESOLUTIONS[self.plugin._settings.get(["pi_cam_resolution"])]
                self.pi_camera.resolution = res_169 if self.plugin._settings.effective['webcam'].get('streamRatio', '4:3') == '16:9' else res_43
                bitrate = bitrate_for_dim(self.pi_camera.resolution[0], self.pi_camera.resolution[1])
                _logger.debug('Pi Camera: framerate: {} - bitrate: {} - resolution: {}'.format(self.pi_camera.framerate, bitrate, self.pi_camera.resolution))
            except picamera.exc.PiCameraError:
                return
        except ModuleNotFoundError:
            _logger.warning('picamera module is not found on a Pi. Seems like an installation error.')
            return

    def video_pipeline(self):
        if not pi_version():
            _logger.warning('Not running on a Pi. Quiting video_pipeline.')
            return

        try:
            if not self.plugin.is_pro_user():
                self.ffmpeg_from_mjpeg()
                return

            compatible_mode = self.plugin._settings.get(["video_streaming_compatible_mode"])

            if compatible_mode == 'auto':
                try:
                    octolapse_enabled = is_octolapse_enabled(self.plugin)
                    if octolapse_enabled:
                        _logger.warning('Octolapse is enabled. Switching to compat mode.')
                        compatible_mode = 'always'
                        alert_queue.add_alert({'level': 'warning', 'cause': 'octolapse_compat_mode'}, self.plugin)
                except Exception:
                    self.sentry.captureException()

            if compatible_mode == 'always':
                self.ffmpeg_from_mjpeg()
                return

            sarge.run('sudo service webcamd stop')

            self.__init_camera__()

            # Use GStreamer for USB Camera. When it's used for Pi Camera it has problems (video is not playing. Not sure why)
            if not self.pi_camera:
                if not os.path.exists('/dev/video0'):
                    _logger.warning('No camera detected. Skipping webcam streaming')
                    return

                _logger.debug('v4l2 device found! Streaming as USB camera.')
                try:
                    bitrate = bitrate_for_dim(640, 480)
                    self.start_ffmpeg('-f v4l2 -s 640x480 -i /dev/video0 -b:v {} -pix_fmt yuv420p -s 640x480 -r 25 -flags:v +global_header -vcodec h264_omx'.format(bitrate), second_stream=' -c:v mjpeg -q:v 1 -s 640x480 -r 5 -an -f mpjpeg udp://127.0.0.1:14498')
                    self.webcam_server = UsbCamWebServer(self.sentry)
                    self.webcam_server.start()
                except Exception:
                    if compatible_mode == 'never':
                        raise
                    self.ffmpeg_from_mjpeg()
                    return

            # Use ffmpeg for Pi Camera. When it's used for USB Camera it has problems (SPS/PPS not sent in-band?)
            else:
                self.start_ffmpeg('-re -i pipe:0 -flags:v +global_header -c:v copy')

                self.webcam_server = PiCamWebServer(self.pi_camera, self.sentry)
                self.webcam_server.start()
                self.pi_camera.start_recording(self.ffmpeg_proc.stdin, format='h264', quality=23, intra_period=25, profile='baseline')
                self.pi_camera.wait_recording(0)
        except Exception:
            alert_queue.add_alert({'level': 'warning', 'cause': 'streaming'}, self.plugin)

            wait_for_port('127.0.0.1', 8080)  # Wait for Flask to start running. Otherwise we will get connection refused when trying to post to '/shutdown'
            self.restore()
            self.sentry.captureException()

    def ffmpeg_from_mjpeg(self):

        @backoff.on_exception(backoff.expo, Exception, jitter=None, max_tries=4)
        def wait_for_webcamd(webcam_settings):
            return capture_jpeg(webcam_settings)

        wait_for_port_to_close('127.0.0.1', 8080)  # wait for WebcamServer to be clear of port 8080
        sarge.run('sudo service webcamd start')

        webcam_settings = self.plugin._settings.global_get(["webcam"])
        jpg = wait_for_webcamd(webcam_settings)
        (_, img_w, img_h) = get_image_info(jpg)
        stream_url = webcam_full_url(webcam_settings.get("stream", "/webcam/?action=stream"))
        bitrate = bitrate_for_dim(img_w, img_h)
        fps = 25
        if not self.plugin.is_pro_user():
            fps = 5
            bitrate = int(bitrate/4)

        self.start_ffmpeg('-re -i {} -filter:v fps={} -b:v {} -pix_fmt yuv420p -s {}x{} -flags:v +global_header -vcodec h264_omx'.format(stream_url, fps, bitrate, img_w, img_h))
        self.compat_streaming = True

    def start_ffmpeg(self, ffmpeg_args, second_stream=None):
        ffmpeg_cmd = '{} {} -bsf dump_extra -an -f rtp rtp://{}:8004?pkt_size=1300'.format(FFMPEG, ffmpeg_args, JANUS_SERVER)
        if second_stream:
            ffmpeg_cmd += second_stream

        _logger.debug('Popen: {}'.format(ffmpeg_cmd))
        FNULL = open(os.devnull, 'w')
        self.ffmpeg_proc = psutil.Popen(ffmpeg_cmd.split(' '), stdin=subprocess.PIPE, stdout=FNULL, stderr=subprocess.PIPE)
        self.ffmpeg_proc.nice(10)

        cpu_watch_dog(self.ffmpeg_proc, self.plugin, max=80, interval=20)

        def monitor_ffmpeg_process():  # It's pointless to restart ffmpeg without calling pi_camera.record with the new input. Just capture unexpected exits not to see if it's a big problem
            ring_buffer = deque(maxlen=50)
            while True:
                err = to_unicode(self.ffmpeg_proc.stderr.readline(), errors='replace')
                if not err:  # EOF when process ends?
                    if self.shutting_down:
                        return

                    returncode = self.ffmpeg_proc.wait()
                    msg = 'STDERR:\n{}\n'.format('\n'.join(ring_buffer))
                    _logger.error(msg)
                    self.sentry.captureMessage('ffmpeg quit! This should not happen. Exit code: {}'.format(returncode))
                    return
                else:
                    ring_buffer.append(err)

        ffmpeg_thread = Thread(target=monitor_ffmpeg_process)
        ffmpeg_thread.daemon = True
        ffmpeg_thread.start()


    def restore(self):
        self.shutting_down = True

        try:
            requests.post('http://127.0.0.1:8080/shutdown')
        except Exception:
            pass

        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.terminate()
            except Exception:
                pass
        if self.pi_camera:
            # https://github.com/waveform80/picamera/issues/122
            try:
                self.pi_camera.stop_recording()
            except Exception:
                pass
            try:
                self.pi_camera.close()
            except Exception:
                pass

        # wait for WebcamServer to be clear of port 8080. Otherwise mjpg-streamer may fail to bind 127.0.0.1:8080 (it can still bind :::8080)
        wait_for_port_to_close('127.0.0.1', 8080)
        sarge.run('sudo service webcamd start')   # failed to start streaming. falling back to mjpeg-streamer

        self.ffmpeg_proc = None
        self.pi_camera = None


class UsbCamWebServer:

    def __init__(self, sentry):
        self.sentry = sentry
        self.web_server = None
        self.mjpeg_ring_buffer = deque(maxlen=512) # 512 * 4 * 1024 bytes hopefully to give enough buffer for the rate mismatch between udp (in) and tcp (out)

    def listen_to_mjpeg_udp_from_ffmepg(self):
        upd_sock = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
        upd_sock.bind(('127.0.0.1', 14498))

        jpeg = bytearray()
        while(True):
            data = upd_sock.recv(4*1024)
            jpeg.extend(data)
            if len(jpeg) > 16 * 1024 * 1024:
                raise Exception('Proper multi-part boundary not detected in ffmepg MJPEG stream.')
            if data[-10:] == '--ffmpeg\r\n': # ffmpeg sends boundary as the last line in a UDP packet to indicate the end of the previous jpeg
                self.mjpeg_ring_buffer.appendleft(jpeg)
                jpeg = bytearray()

    def mjpeg_generator(self):
        try:
            while True:
                if self.mjpeg_ring_buffer:
                    yield bytes(self.mjpeg_ring_buffer.pop())
                else:
                    time.sleep(0.01)
        except GeneratorExit:
            pass
        except Exception:
            self.sentry.captureException()
            raise

    def get_mjpeg(self):
        return flask.Response(flask.stream_with_context(self.mjpeg_generator()), mimetype='multipart/x-mixed-replace;boundary=ffmpeg')

    def get_snapshot(self):
        data = None
        for i in range(100):
            if self.mjpeg_ring_buffer:
                data = self.mjpeg_ring_buffer.pop()
                break
            time.sleep(0.01)

        if not data:
            return flask.Response(status=500)

        start_marker = data.find(b'\xff\xd8\xff')
        if start_marker < 0:
            return flask.Response(status=500)

        end_marker = data.find(b'\xff\xd9')
        if end_marker > 0:
            return flask.send_file(io.BytesIO(data[start_marker:end_marker+2]), mimetype='image/jpeg')
        else:
            return flask.send_file(io.BytesIO(data[start_marker:]), mimetype='image/jpeg') # end_marker not found. Better way to handle it?

    def run_forever(self):
        webcam_server_app = flask.Flask('webcam_server')

        @webcam_server_app.route('/')
        def webcam():
            action = flask.request.args['action']
            if action == 'snapshot':
                return self.get_snapshot()
            else:
                return self.get_mjpeg()

        @webcam_server_app.route('/shutdown', methods=['POST'])
        def shutdown():
            flask.request.environ.get('werkzeug.server.shutdown')()
            return 'Ok'

        webcam_server_app.run(port=8080, threaded=True)

    def start(self):
        cam_server_thread = Thread(target=self.run_forever)
        cam_server_thread.daemon = True
        cam_server_thread.start()
        self.listen_to_mjpeg_udp_from_ffmepg()


class PiCamWebServer:
    def __init__(self, camera, sentry):
        self.sentry = sentry
        self.pi_camera = camera
        self.img_q = queue.Queue(maxsize=1)
        self.last_capture = 0
        self._mutex = RLock()
        self.web_server = None

    def capture_forever(self):
        try:
            bio = io.BytesIO()
            for foo in self.pi_camera.capture_continuous(bio, format='jpeg', use_video_port=True):
                bio.seek(0)
                chunk = bio.read()
                bio.seek(0)
                bio.truncate()

                with self._mutex:
                    last_last_capture = self.last_capture  # noqa: F841 for sentry?
                    self.last_capture = time.time()

                self.img_q.put(chunk)
        except Exception:
            self.sentry.captureException()
            raise

    def mjpeg_generator(self, boundary):
        try:
            hdr = '--%s\r\nContent-Type: image/jpeg\r\n' % boundary

            prefix = ''
            while True:
                chunk = self.img_q.get()
                msg = prefix + hdr + 'Content-Length: {}\r\n\r\n'.format(len(chunk))
                yield msg.encode('iso-8859-1') + chunk
                prefix = '\r\n'
                time.sleep(0.15)  # slow down mjpeg streaming so that it won't use too much cpu or bandwidth
        except GeneratorExit:
            pass
        except Exception:
            self.sentry.captureException()
            raise

    def get_snapshot(self):
        possible_stale_pics = 3
        while True:
            chunk = self.img_q.get()
            with self._mutex:
                gap = time.time() - self.last_capture
                if gap < 0.1:
                    possible_stale_pics -= 1      # Get a few pics to make sure we are not returning a stale pic, which will throw off Octolapse
                    if possible_stale_pics <= 0:
                        break

        return flask.send_file(io.BytesIO(chunk), mimetype='image/jpeg')

    def get_mjpeg(self):
        boundary = 'herebedragons'
        return flask.Response(flask.stream_with_context(self.mjpeg_generator(boundary)), mimetype='multipart/x-mixed-replace;boundary=%s' % boundary)

    def run_forever(self):
        webcam_server_app = flask.Flask('webcam_server')

        @webcam_server_app.route('/')
        def webcam():
            action = flask.request.args['action']
            if action == 'snapshot':
                return self.get_snapshot()
            else:
                return self.get_mjpeg()

        @webcam_server_app.route('/shutdown', methods=['POST'])
        def shutdown():
            flask.request.environ.get('werkzeug.server.shutdown')()
            return 'Ok'

        webcam_server_app.run(port=8080, threaded=True)

    def start(self):
        cam_server_thread = Thread(target=self.run_forever)
        cam_server_thread.daemon = True
        cam_server_thread.start()

        capture_thread = Thread(target=self.capture_forever)
        capture_thread.daemon = True
        capture_thread.start()
