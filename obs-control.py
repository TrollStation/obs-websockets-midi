from tornado import gen
from tornado import httpclient
from tornado import ioloop
from tornado import websocket

import json
import logging
import base64
import hashlib
import mido
from configparser import ConfigParser
import mido.backends.rtmidi

Config = ConfigParser()
Config.read('obs-control.conf')
HOST = Config.get('OBS_WebSockets', 'host', fallback='127.0.0.1')
PORT = Config.get('OBS_WebSockets', 'port', fallback='4444')
PASSWORD = Config.get('OBS_WebSockets', 'password', fallback=None)

ERROR = -1
STARTING = 1
STARTED = 2
STOPPING = 3
STOPPED = 4


def setup_logger(level):
    logging.basicConfig(format='%(asctime)s [%(levelname)s] : %(message)s',
                        filename='obs-control.log',
                        level=level)
    log = logging.getLogger('obs-control')
    log.setLevel(level)
    stdout_lh = logging.StreamHandler()
    stdout_lh.setLevel(level)
    stdout_lh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] : %(message)s'))
    log.addHandler(stdout_lh)
    return log

log = setup_logger(Config.getint('OBS_Control', 'log_level', fallback=20))
mido.set_backend(Config.get('MIDI_Settings', 'midi_backend', fallback='mido.backends.rtmidi'))


class MIDIMapping:
    def __init__(self, filename):
        self.filename = filename
        self.config = None
        self.reset_controller = []
        self.init_sequence = []
        self.stream_toggle = None
        self.record_toggle = None
        self.scenes = []
        self.sources = []
        self.load_file()
        self.set_mapping()

    def load_file(self):
        try:
            with open(self.filename, 'r') as f:
                self.config = json.load(f)
                log.info('MIDI mapping {0} loaded'.format(self.filename))
        except ValueError as e:
            log.critical('MIDI mapping reading failed')
            print(e)
            exit(1)

    def set_mapping(self):
        self.set_reset_sequence(self.config['reset'])
        self.set_init_sequence(self.config['init'])
        self.set_record_toggle(self.config['record'])
        self.set_stream_toggle(self.config['stream'])
        self.set_scenes(self.config['scenes'])
        self.set_sources(self.config['sources'])

    def set_reset_sequence(self, data):
        for msg in data:
            self.reset_controller.append(self.midi_message(msg))

    def set_init_sequence(self, data):
        for msg in data:
            self.init_sequence.append(self.midi_message(msg))

    def set_scenes(self, data):
        class MIDIMapSceneState:
            def __init__(self, index, missing, inactive, active, transition, pending, trigger):
                self.scene_index = index
                self.missing = missing
                self.inactive = inactive
                self.active = active
                self.transition = transition
                self.pending = pending
                self.trigger = trigger
        for scene in data:
            self.scenes.append(MIDIMapSceneState(
                int(scene['index']),
                self.midi_message(scene['missing']),
                self.midi_message(scene['inactive']),
                self.midi_message(scene['active']),
                self.midi_message(scene['transition']),
                self.midi_message(scene['pending']),
                self.midi_message(scene['switch']))
            )

    def set_sources(self, data):
        pass

    def set_record_toggle(self, state):
        class MIDIMapRecordState:
            def __init__(self, inactive, active, pending, trigger):
                self.inactive = inactive
                self.active = active
                self.pending = pending
                self.trigger = trigger
        self.record_toggle = MIDIMapRecordState(
            self.midi_message(state['inactive']),
            self.midi_message(state['active']),
            self.midi_message(state['pending']),
            self.midi_message(state['toggle'])
        )

    def set_stream_toggle(self, state):
        class MIDIMapStreamState:
            def __init__(self, inactive, active, pending, trigger):
                self.inactive = inactive
                self.active = active
                self.pending = pending
                self.trigger = trigger
        self.stream_toggle = MIDIMapStreamState(
            self.midi_message(state['inactive']),
            self.midi_message(state['active']),
            self.midi_message(state['pending']),
            self.midi_message(state['toggle'])
        )

    def get_scene_mapping_by_index(self, index):
        for scene in self.scenes:
            if scene.scene_index == index:
                return scene
        return False

    def midi_message(self, data):
        if data['type'] == 'note_on':
            return self.note_on_message(data['channel'], data['note'], data['velocity'])
        elif data['type'] == 'note_off':
            return self.note_off_message(data['channel'], data['note'], data['velocity'])
        elif data['type'] == 'control_change':
            return self.control_change_message(data['channel'], data['control'], data['value'])
        else:
            log.error('MIDI message type {0} is not supported'.format(data['type']))
            exit(4)

    @staticmethod
    def note_on_message(channel, note, velocity):
        return mido.Message('note_on', channel=channel, note=note, velocity=velocity)

    @staticmethod
    def note_off_message(channel, note, velocity):
        return mido.Message('note_off', channel=channel, note=note, velocity=velocity)

    @staticmethod
    def control_change_message(channel, control, value):
        return mido.Message('control_change', channel=channel, control=control, value=value)


class MIDIControl:
    def __init__(self):
        self.in_port = None
        self.out_port = None
        self.open_ports()
        self.mapping = MIDIMapping(Config.get('MIDI_Settings', 'mapping_file', fallback='midi-mapping.json'))
        self.scene_triggers = []
        self.source_triggers = []
        if Config.getboolean('MIDI_Settings', 'reset_controller', fallback=False):
            self.send_reset_controller()
        if Config.getboolean('MIDI_Settings', 'init_sequence', fallback=False):
            self.send_init_sequence()

    def open_ports(self):
        log.debug('Trying open MIDI ports')
        try:
            self.in_port = mido.open_input(Config.get('MIDI_Settings', 'input_port'), callback=self.receive_message)
        except OSError:
            log.critical('MIDI Input port opening failed')
            print('MIDI INs:', mido.get_input_names())
            exit(2)
        try:
            self.out_port = mido.open_output(Config.get('MIDI_Settings', 'output_port'))
        except OSError:
            log.critical('MIDI Output port opening failed')
            print('MIDI OUTs: ', mido.get_output_names())
            exit(3)

    def send_message(self, message):
        # TODO: Catch exceptions
        self.out_port.send(message)
        log.debug('Sent MIDI message: {0}'.format(message))
        if Config.getboolean('OBS_Control', 'dump_midi_proto', fallback=False):
            print(message)

    def receive_message(self, message):
        log.debug('Received MIDI Message')
        if Config.getboolean('OBS_Control', 'dump_midi_proto', fallback=False):
            print(message)
        ioloop.IOLoop().instance().spawn_callback(self.process_message, message)

    def process_message(self, message):
        scene_trigger = self.check_scene_triggers(message)
        if scene_trigger:
            self.send_scene_transition_state(scene_trigger.scene_index)
            OBS.set_current_scene_by_index(scene_trigger.scene_index)
        source_trigger = self.check_source_triggers(message)
        if source_trigger:
            pass  # OBS.Control.do_something_with_source
        if self.mapping.record_toggle.trigger == message:
            OBS.record_toggle()
        if self.mapping.stream_toggle.trigger == message:
            OBS.stream_toggle()

    def check_scene_triggers(self, message):
        for scene in self.mapping.scenes:
            if scene.trigger == message:
                return scene
        return False

    def check_source_triggers(self, message):
        return False

    def send_reset_controller(self):
        log.info('Sending reset controller sequence')
        for message in self.mapping.reset_controller:
            self.send_message(message)

    def send_init_sequence(self):
        log.info('Sending initial sequence')
        for message in self.mapping.init_sequence:
            self.send_message(message)

    def send_record_state(self):
        if OBS.recording_state == STARTING:
            self.send_message(self.mapping.record_toggle.pending)
        elif OBS.recording_state == STARTED:
            self.send_message(self.mapping.record_toggle.active)
        elif OBS.recording_state == STOPPING:
            self.send_message(self.mapping.record_toggle.pending)
        elif OBS.recording_state == STOPPED:
            self.send_message(self.mapping.record_toggle.inactive)
        else:
            log.error('WRONG RECORDING STATE')

    def send_stream_state(self):
        if OBS.streaming_state == STARTING:
            self.send_message(self.mapping.stream_toggle.pending)
        elif OBS.streaming_state == STARTED:
            self.send_message(self.mapping.stream_toggle.active)
        elif OBS.streaming_state == STOPPING:
            self.send_message(self.mapping.stream_toggle.pending)
        elif OBS.streaming_state == STOPPED:
            self.send_message(self.mapping.stream_toggle.inactive)
        else:
            log.error('WRONG STREAMING STATE')

    def send_scenes_state(self):
        current_scene_index = OBS.get_current_scene_index()
        for scene in self.mapping.scenes:
            if scene.scene_index == current_scene_index:
                self.send_message(scene.active)
            elif scene.scene_index <= (len(OBS.scenes) - 1):
                self.send_message(scene.inactive)
            elif scene.scene_index > (len(OBS.scenes) - 1):
                self.send_message(scene.missing)

    def send_scene_pending_state(self, index):
        for scene in self.mapping.scenes:
            if scene.scene_index == index:
                self.send_message(scene.pending)

    def send_scene_transition_state(self, index):
        for scene in self.mapping.scenes:
            if scene.scene_index == index:
                self.send_message(scene.transition)

    def send_source_state(self, state):
        pass

    def send_obs_state(self):
        self.send_record_state()
        self.send_scenes_state()
        self.send_stream_state()

    def close_ports(self):
        if self.in_port:
            self.in_port.close()
        if self.out_port:
            self.out_port.close()
        log.debug('MIDI ports closed')

    def __del__(self):
        self.close_ports()


class OBSControl:
    def __init__(self, host, port, password=None):
        log.info('=== START ===')
        self.ws = OBSWebSocketClient()
        self.ws.connect('ws://{0}:{1}'.format(host, port))
        self.midi = MIDIControl()
        self.password = password
        self.scenes = []
        self.current_scene = None
        self.transitions = []
        self.current_transition = None
        self.current_transition_duration = None
        self.streaming_state = STOPPED
        self.recording_state = STOPPED

    def send_request(self, method, msg_id=None, data=None):
        request = dict()
        request['request-type'] = method
        if msg_id:
            request['message-id'] = msg_id
        if data:
            request.update(data)
            # request = {**request, **data}
        result = json.dumps(request, indent='\t')
        self.ws.send(result)
        log.debug('Request sent')
        if Config.getboolean('OBS_Control', 'dump_websockets_proto', fallback=False):
            print(result)

    def process_response(self, resp):
        log.debug('Response received')
        if Config.getboolean('OBS_Control', 'dump_websockets_proto', fallback=False):
            print(resp)
        resp = json.loads(resp)
        if 'status' in resp.keys():
            if resp['status'] == 'error':
                log.error('OBS said: ' + resp['error'])
        if 'message-id' in resp.keys():
            self.process_message_id(resp)
        if 'update-type' in resp.keys():
            self.process_update(resp)

    def process_message_id(self, resp):
        if resp['message-id'] == 'AUTH_REQ':
            if resp['authRequired']:
                self.authenticate(self.password, resp['challenge'], resp['salt'])
            else:
                self.init_state()
        elif resp['message-id'] == 'AUTH_TRY':
            if resp['status'] == 'ok':
                self.init_state()
            else:
                self.exit()
        elif resp['message-id'] == 'SCENE_LIST':
            self.update_scene_list(resp)
        elif resp['message-id'] == 'SET_SCENE':
            self.midi.send_scene_pending_state(self.get_current_scene_index())
        elif resp['message-id'] == 'STREAM_RECORD_STATUS':
            self.update_stream_record_status(resp)
        elif resp['message-id'] == 'GET_TRANSITION_LIST':
            self.update_transition_list(resp)
        elif resp['message-id'] == '':
            pass
        else:
            log.warning('Unhandled message')
            if not Config.getboolean('OBS_Control', 'dump_websockets_proto', fallback=False):
                print(resp)

    def process_update(self, resp):
        log.info('Processing update')
        if resp['update-type'] == 'SwitchScenes':
            self.current_scene = resp['scene-name']
            self.midi.send_scenes_state()
        elif resp['update-type'] == 'ScenesChanged':
            self.get_scene_list()
        elif resp['update-type'] == 'RecordingStarting':
            self.recording_state = STARTING
        elif resp['update-type'] == 'RecordingStarted':
            self.recording_state = STARTED
        elif resp['update-type'] == 'RecordingStopping':
            self.recording_state = STOPPING
        elif resp['update-type'] == 'RecordingStopped':
            self.recording_state = STOPPED
        elif resp['update-type'] == 'StreamStarting':
            self.streaming_state = STARTING
        elif resp['update-type'] == 'StreamStarted':
            self.streaming_state = STARTED
        elif resp['update-type'] == 'StreamStopping':
            self.streaming_state = STOPPING
        elif resp['update-type'] == 'StreamStopped':
            self.streaming_state = STOPPED
        else:
            log.warning('Unhandled message')
            if not Config.getboolean('OBS_Control', 'dump_websockets_proto', fallback=False):
                print(resp)
        self.midi.send_record_state()
        self.midi.send_stream_state()

    def get_auth_required(self):
        self.send_request('GetAuthRequired', 'AUTH_REQ')

    def authenticate(self, password, challenge, salt):
        log.info('Trying authorize...')
        secret_string = password + salt
        secret_hash = hashlib.sha256(secret_string.encode('utf-8')).digest()
        secret = base64.b64encode(secret_hash).decode('utf-8')
        auth_response_string = secret + challenge
        auth_response_hash = hashlib.sha256(auth_response_string.encode('utf-8')).digest()
        auth_response = base64.b64encode(auth_response_hash).decode('utf-8')
        self.send_request('Authenticate', 'AUTH_TRY', {'auth': auth_response})

    def init_state(self):
        log.info('Loading init state')
        self.get_scene_list()
        self.get_transition_list()
        self.get_stream_record_status()

    def get_scene_list(self):
        log.info('Getting scene list')
        self.send_request('GetSceneList', 'SCENE_LIST')

    def get_current_scene_index(self):
        log.debug('Getting current scene index')
        for scene in self.scenes:
            if self.current_scene == scene['name']:
                return self.scenes.index(scene)

    def update_scene_list(self, resp):
        log.info('Updating scene list')
        self.scenes = resp['scenes']
        self.current_scene = resp['current-scene']
        self.midi.send_scenes_state()

    def set_current_scene(self, name):
        log.info('Setting scene {0}'.format(name))
        self.send_request('SetCurrentScene', 'SET_SCENE', {'scene-name': name})

    def set_current_scene_by_index(self, scene_index):
        log.debug('Setting scene by index {0}'.format(scene_index))
        for scene in self.scenes:
            if scene_index == self.scenes.index(scene):
                self.set_current_scene(scene['name'])
                return
        log.error('No scene with index {0}'.format(scene_index))

    def get_stream_record_status(self):
        log.info('Getting stream/record status')
        self.send_request('GetStreamingStatus', 'STREAM_RECORD_STATUS')

    def update_stream_record_status(self, resp):
        log.info('Updating stream/record status')
        if resp['streaming']:
            self.streaming_state = STARTED
        else:
            self.streaming_state = STOPPED
        if resp['recording']:
            self.recording_state = STARTED
        else:
            self.recording_state = STOPPED
        self.midi.send_record_state()
        self.midi.send_stream_state()

    def record_toggle(self):
        log.info('Toggle record state')
        self.send_request('StartStopRecording')

    def stream_toggle(self):
        log.info('Toggle stream state')
        self.send_request('StartStopStreaming')

    def get_transition_list(self):
        self.send_request('GetTransitionList', 'GET_TRANSITION_LIST')

    def get_current_transition(self):
        self.send_request('GetCurrentTransition', 'GET_CURRENT_TRANSITION')

    def update_transition_list(self, resp):
        log.info('Updating transitions list')
        self.transitions = resp['transitions']
        self.current_transition = resp['current-transition']

    def update_current_transition(self, resp):
        self.current_transition = resp['name']
        self.current_transition_duration = resp['duration']

    def exit(self):
        log.info('=== STOP ===')
        exit(0)


class WebSocketClient:
    def __init__(self, *, connect_timeout=Config.getint('OBS_Control', 'connect_timeout', fallback=10),
                 request_timeout=Config.getint('OBS_Control', 'request_timeout', fallback=10)):
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout

    def connect(self, url):
        request = httpclient.HTTPRequest(url=url,
                                         connect_timeout=self.connect_timeout,
                                         request_timeout=self.request_timeout)
        ws_conn = websocket.WebSocketClientConnection(ioloop.IOLoop.current(),
                                                      request)
        ws_conn.connect_future.add_done_callback(self._connect_callback)

    def send(self, data):
        if not self._ws_connection:
            raise RuntimeError('Web socket connection is closed.')
        self._ws_connection.write_message(data)

    def close(self):
        if not self._ws_connection:
            raise RuntimeError('Web socket connection is already closed.')
        self._ws_connection.close()

    def _connect_callback(self, future):
        if future.exception() is None:
            self._ws_connection = future.result()
            self._on_connection_success()
            self._read_messages()
        else:
            self._on_connection_error(future.exception())

    @gen.coroutine
    def _read_messages(self):
        while True:
            msg = yield self._ws_connection.read_message()
            if msg is None:
                self._on_connection_close()
                break
            self._on_message(msg)

    def _on_message(self, msg):
        pass

    def _on_connection_success(self):
        pass

    def _on_connection_close(self):
        pass

    def _on_connection_error(self, exception):
        pass

    def __del__(self):
        if True:
            self.close()


class OBSWebSocketClient(WebSocketClient):
    def _on_message(self, msg):
        ioloop.IOLoop.instance().spawn_callback(OBS.process_response, msg)

    def _on_connection_success(self):
        log.info('Connection success')
        ioloop.IOLoop().instance().spawn_callback(OBS.get_auth_required)

    def _on_connection_close(self):
        log.info('Connection closed')
        exit(0)

    def _on_connection_error(self, exception):
        log.error('Connection error: {0}'.format(exception))
        exit(0)

if __name__ == '__main__':
    OBS = OBSControl(HOST, PORT, PASSWORD)
    tlog = logging.getLogger('tornado')
    tlog_lh = logging.StreamHandler()
    tlog_lh.setLevel(logging.DEBUG)
    # tlog_lh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] : %(message)s'))
    tlog.addHandler(tlog_lh)
    try:
        ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        OBS.exit()
