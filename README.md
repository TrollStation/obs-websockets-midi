# OBS-Websockets-MIDI Bridge

Console application to control OpenBroadcaster Studio via MIDI through websockets.
Windows and Linux, no MacOS tested.
Version 0.1

## Requirements
* python 3.6
* tornado
* mido
* python-rtmidi
* obs-websockets plugin for OBS

## Supported
* Scene switching and state MIDI-feedback
* Streaming control and state MIDI-feedback
* Recording control and state MIDI-feedback

## Usage
Start with
    python obs-control.py
or download cxfreeze-compiled version if you don't have python interpetator.
If your config has wrong values for **input_port** or **output_port** then app will print input or output devices and exit. This prints must be copied to config file.

## Config options
### Section OBS_Control:
* **dump_websockets_proto** *bool* dumps websockets messages to stdout
* **dump_midi_proto** *bool* dumps MIDI messages to stdout
* **log_level** *int* sets a logging level

### Section OBS_WebSockets:
* **host** *str* WebSocket server host to connect
* **port** *int* WebSocket server port listen
* **password** *str* WebSocket server password
* **connect_timeout** *int* connection timeout in seconds
* **request_timeout** *int* timeout per websockets request in seconds
 
### Section MIDI_Settings:
* **midi_backend** *str* sets MIDI backend module for *mido*
* **input_port** *str* MIDI device name for receiving messages
* **output_port** *str* MIDI device name for sending messages
* **mapping_file** *str* file contains MIDI-mappings
* **reset_controller** *bool* toggle sending reset controller sequence defined in mapping
* **init_sequence** *bool* toggle sending initial sequence defined in mapping

## Mapping format
Mapping file is *JSON* representation of MIDI messages. It's specific for type of message, but very simple. 
Typical message has **type**(now supported *note_on*, *note_off*, *control_change*), **channel** for MIDI-channel, **note**, **velocity** for *note_on*/*note_off* type and **control**, **value** for *control_change* type.
### **init** section
Contains list of messages which will be sent after resetting MIDI-controller

### **reset** section
Contains list of messages which necessary to reset MIDI-controller

### **record** and **stream** section
Contains messages to interact with OBS record or stream control:
* **inactive** message will be sent when OBS is not recording/streaming
* **active** message sends when OBS is recording/streaming
* **pending** message sends when OBS is starting or stopping recording/streaming
* **toggle** message will be listen from MIDI-controller to control recording/streaming state

### **scenes** section
Contains list of message to interact with scenes. List allocates button to show state of scenes and switching between.
* **index** points to scene location in OBS scenes list, begins from 0.
* **missing** message will be sent when in OBS scenes list not found specified **index**
* **inactive** message used for indicate scene exists and ready for switch up
* **active** message used for indicate active scene for now
* **transition** message lights up coming scene while runs transition
* **pending** lights up previous scene while runs transition
* **switch**  message will be listen from MIDI-controller to send scene switching requests