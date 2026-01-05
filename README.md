# RPI Living Room Media Bridge

A lightweight MQTT bridge that exposes your living room media system (TV + audio) to Home Assistant, enabling reliable local automations based on power, volume, and playback state.

## Features

- **TV Power State** via HDMI-CEC → `media/living_room/tv/on`
- **Volume Control** via HDMI-CEC → `media/living_room/audio/volume`, `muted`
- **Playback State** from spotifyd/AirPlay → `media/living_room/playback/state`, `source`
- **Command Endpoints** for volume control and playback
- **LWT Availability** tracking for Home Assistant

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Raspberry Pi                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ │
│  │ CEC Listener│  │Audio Listener│ │    MQTT Client      │ │
│  │  (TV/Vol)   │  │(spotifyd/AP)│  │   (HA Bridge)       │ │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘ │
│         │                │                     │            │
│         └────────────────┴─────────────────────┘            │
│                          │                                  │
└──────────────────────────┼──────────────────────────────────┘
                           │ MQTT
                           ▼
              ┌────────────────────────┐
              │   Home Assistant       │
              │   (Mosquitto Broker)   │
              └────────────────────────┘
```

## Requirements

- Raspberry Pi with HDMI-CEC support (built-in on most Pi models)
- Python 3.11+
- MQTT broker (Home Assistant Mosquitto add-on recommended)
- Optional: spotifyd, shairport-sync for audio source detection

## Installation

### 1. Clone and Install

```bash
cd /home/pi
git clone https://github.com/yourusername/media-bridge.git
cd media-bridge

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install
pip install -e .

# For audio source detection (optional)
pip install dbus-python
```

### 2. Configure

```bash
# Create config directory
sudo mkdir -p /etc/media-bridge

# Copy and edit config
sudo cp config.example.yaml /etc/media-bridge/config.yaml
sudo nano /etc/media-bridge/config.yaml
```

Minimum required configuration:

```yaml
mqtt:
  host: "homeassistant.local"  # Your HA hostname/IP
  username: "mqtt-user"
  password: "mqtt-password"
```

### 3. Test

```bash
# Run manually to test
media-bridge --config /etc/media-bridge/config.yaml -v
```

### 4. Install Service

```bash
# Copy service file
sudo cp systemd/media-bridge.service /etc/systemd/system/

# Edit paths if needed
sudo nano /etc/systemd/system/media-bridge.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable media-bridge
sudo systemctl start media-bridge

# Check status
sudo systemctl status media-bridge
journalctl -u media-bridge -f
```

## MQTT Topics

### State Topics (Published by Bridge)

| Topic | Payload | Description |
|-------|---------|-------------|
| `media/living_room/availability` | `online`/`offline` | Bridge availability (LWT) |
| `media/living_room/tv/on` | `true`/`false` | TV power state |
| `media/living_room/audio/volume` | `0-100` | Current volume level |
| `media/living_room/audio/muted` | `true`/`false` | Mute state |
| `media/living_room/playback/state` | `idle`/`playing`/`paused` | Playback state |
| `media/living_room/playback/source` | `spotify`/`airplay`/`tv`/`unknown` | Active source |
| `media/living_room/playback/title` | string | Current track title |
| `media/living_room/playback/artist` | string | Current artist |

### Command Topics (Subscribed by Bridge)

| Topic | Payload | Description |
|-------|---------|-------------|
| `media/living_room/audio/set_volume` | `0-100` | Set volume level |
| `media/living_room/audio/volume_up` | (empty) | Increase volume |
| `media/living_room/audio/volume_down` | (empty) | Decrease volume |
| `media/living_room/audio/mute` | (empty) | Mute audio |
| `media/living_room/audio/unmute` | (empty) | Unmute audio |
| `media/living_room/playback/play` | (empty) | Resume playback |
| `media/living_room/playback/pause` | (empty) | Pause playback |
| `media/living_room/playback/stop` | (empty) | Stop playback |

## Home Assistant Configuration

### MQTT Sensors

Add to your `configuration.yaml`:

```yaml
mqtt:
  binary_sensor:
    - name: "Living Room TV"
      state_topic: "media/living_room/tv/on"
      payload_on: "true"
      payload_off: "false"
      device_class: power
      availability:
        - topic: "media/living_room/availability"
          payload_available: "online"
          payload_not_available: "offline"

  sensor:
    - name: "Living Room Volume"
      state_topic: "media/living_room/audio/volume"
      unit_of_measurement: "%"
      availability:
        - topic: "media/living_room/availability"

    - name: "Living Room Playback State"
      state_topic: "media/living_room/playback/state"
      availability:
        - topic: "media/living_room/availability"

    - name: "Living Room Media Source"
      state_topic: "media/living_room/playback/source"
      availability:
        - topic: "media/living_room/availability"
```

### Example Automations

**Movie Mode** - Dim lights when TV turns on:

```yaml
automation:
  - alias: "Living Room Movie Mode"
    trigger:
      - platform: state
        entity_id: binary_sensor.living_room_tv
        to: "on"
    action:
      - service: light.turn_on
        target:
          entity_id: light.living_room
        data:
          brightness_pct: 20
```

**Night Mode** - Cap volume after 10pm:

```yaml
automation:
  - alias: "Living Room Night Volume Cap"
    trigger:
      - platform: mqtt
        topic: "media/living_room/audio/volume"
    condition:
      - condition: time
        after: "22:00:00"
        before: "07:00:00"
      - condition: template
        value_template: "{{ trigger.payload | int > 30 }}"
    action:
      - service: mqtt.publish
        data:
          topic: "media/living_room/audio/set_volume"
          payload: "30"
```

**Sleep Mode** - Pause media when going to bed:

```yaml
automation:
  - alias: "Pause Media on Sleep"
    trigger:
      - platform: state
        entity_id: binary_sensor.eight_sleep_in_bed
        to: "on"
    action:
      - service: mqtt.publish
        data:
          topic: "media/living_room/playback/pause"
```

## Environment Variables

All config options can be overridden with environment variables:

| Variable | Description |
|----------|-------------|
| `MQTT_HOST` | MQTT broker hostname |
| `MQTT_PORT` | MQTT broker port |
| `MQTT_USERNAME` | MQTT username |
| `MQTT_PASSWORD` | MQTT password |
| `MQTT_TOPIC_PREFIX` | Topic prefix |
| `CEC_ENABLED` | Enable CEC (true/false) |
| `CEC_DEVICE` | CEC device path |
| `AUDIO_ENABLED` | Enable audio listener (true/false) |
| `LOG_LEVEL` | Log level (DEBUG/INFO/WARNING/ERROR) |

## Troubleshooting

### CEC Not Working

```bash
# Check CEC device exists
ls -la /dev/cec*

# Test CEC with cec-client
echo 'scan' | cec-client -s -d 1

# Ensure user is in video group
sudo usermod -a -G video pi
```

### MQTT Connection Issues

```bash
# Test MQTT connection
mosquitto_pub -h homeassistant.local -u user -P pass -t test -m "hello"

# Check logs
journalctl -u media-bridge -f
```

### D-Bus Issues (Audio Listener)

```bash
# Check D-Bus session
echo $DBUS_SESSION_BUS_ADDRESS

# For systemd service, may need to set:
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
```

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black src/
ruff check src/ --fix
```

## License

MIT

