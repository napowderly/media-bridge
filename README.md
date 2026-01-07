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

## Quick Start (Existing System)

If you just need to install the media-bridge on a system that already has spotifyd/shairport-sync configured, skip to [Install Media Bridge](#3-install-media-bridge).

## Full System Setup (Fresh Pi)

Complete instructions for provisioning a new Raspberry Pi from scratch.

### 1. Base System Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install system dependencies
sudo apt install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    cec-utils \
    git

# Add user to required groups
sudo usermod -a -G video,audio $USER

# Reboot to apply group changes
sudo reboot
```

### 2. Install Audio Services

#### 2a. Install spotifyd (Spotify Connect)

spotifyd makes your Pi appear as a Spotify Connect speaker.

```bash
# Install spotifyd from apt (Debian/Raspbian)
sudo apt install -y spotifyd

# Or install latest from GitHub releases (recommended for Pi)
wget https://github.com/Spotifyd/spotifyd/releases/latest/download/spotifyd-linux-armhf-slim.tar.gz
tar -xzf spotifyd-linux-armhf-slim.tar.gz
sudo mv spotifyd /usr/local/bin/
rm spotifyd-linux-armhf-slim.tar.gz
```

Create spotifyd configuration:

```bash
mkdir -p ~/.config/spotifyd
cat > ~/.config/spotifyd/spotifyd.conf << 'EOF'
[global]
# Your Spotify username (not email)
username = "YOUR_SPOTIFY_USERNAME"

# Your Spotify password (or use password_cmd for security)
password = "YOUR_SPOTIFY_PASSWORD"

# Or use password from file/command:
# password_cmd = "cat /home/pi/.spotify_password"
# password_cmd = "pass show spotify"

# Device name shown in Spotify
device_name = "Living Room"

# Audio device (use 'aplay -L' to list devices)
# For USB DAC or HDMI:
device = "default"
# For specific ALSA device:
# device = "hw:0,0"

# Audio backend
backend = "alsa"

# Audio bitrate (96, 160, 320)
bitrate = 320

# Volume controller
volume_controller = "alsa"
# Or use softvol for software volume:
# volume_controller = "softvol"

# Enable D-Bus MPRIS (required for media-bridge detection!)
use_mpris = true

# Normalize volume
volume_normalisation = true
normalisation_pregain = -10

# Cache for offline (optional)
cache_path = "/home/pi/.cache/spotifyd"

# Don't use Zeroconf (we set credentials above)
zeroconf_port = 0
EOF
```

Create systemd service for spotifyd:

```bash
sudo cat > /etc/systemd/system/spotifyd.service << 'EOF'
[Unit]
Description=Spotify Connect daemon
Documentation=https://github.com/Spotifyd/spotifyd
After=network-online.target sound.target
Wants=network-online.target sound.target

[Service]
Type=simple
User=pi
Group=pi
ExecStart=/usr/local/bin/spotifyd --no-daemon --config-path /home/pi/.config/spotifyd/spotifyd.conf
Restart=always
RestartSec=10

# D-Bus session access (required for MPRIS)
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable spotifyd
sudo systemctl start spotifyd

# Verify it's running
systemctl status spotifyd
```

#### 2b. Install shairport-sync (AirPlay)

shairport-sync makes your Pi appear as an AirPlay receiver.

```bash
# Install shairport-sync with D-Bus/MPRIS support
sudo apt install -y shairport-sync

# Verify D-Bus support is compiled in
shairport-sync -V
# Should show: "with metadata support" and "with dbus support"
```

Configure shairport-sync:

```bash
sudo nano /etc/shairport-sync.conf
```

Update the configuration:

```conf
// General settings
general = {
    name = "Living Room";           // AirPlay device name
    interpolation = "basic";        // or "soxr" for higher quality
    output_backend = "alsa";        // Audio backend
    mdns_backend = "avahi";         // Service discovery
    port = 5000;                    // AirPlay port
    drift_tolerance_in_seconds = 0.002;
    resync_threshold_in_seconds = 0.050;
};

// ALSA output settings
alsa = {
    output_device = "default";      // Use 'aplay -L' to list devices
    // output_device = "hw:0,0";    // For specific device
    mixer_control_name = "PCM";     // Volume control (use 'amixer' to find)
    // mixer_control_name = "Master";
};

// Session control - important for metadata!
sessioncontrol = {
    run_this_before_play_begins = "/usr/bin/logger 'AirPlay started'";
    run_this_after_play_ends = "/usr/bin/logger 'AirPlay stopped'";
    wait_for_completion = "no";
};

// D-Bus interface (required for media-bridge!)
dbus = {
    enabled = "yes";
    bus_type = "session";           // Use session bus for MPRIS
    service_name = "org.gnome.ShairportSync";
    mpris_service_name = "ShairportSync";
};

// Metadata publishing
metadata = {
    enabled = "yes";
    include_cover_art = "yes";
    cover_art_cache_directory = "/tmp/shairport-sync/.cache/coverart";
    pipe_name = "/tmp/shairport-sync-metadata";
    pipe_timeout = 5000;
};

// Diagnostics (optional, for troubleshooting)
diagnostics = {
    // log_verbosity = 2;
    // statistics = "yes";
};
```

Create/update the systemd service for session D-Bus:

```bash
# Create override directory
sudo mkdir -p /etc/systemd/system/shairport-sync.service.d

# Add D-Bus session environment
sudo cat > /etc/systemd/system/shairport-sync.service.d/override.conf << 'EOF'
[Service]
User=pi
Group=pi
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
EOF

# Reload and restart
sudo systemctl daemon-reload
sudo systemctl enable shairport-sync
sudo systemctl restart shairport-sync

# Verify it's running
systemctl status shairport-sync
```

#### 2c. Ensure D-Bus Session Bus Starts at Boot

The media-bridge needs the D-Bus session bus to detect playback. Enable lingering for your user:

```bash
# Enable user services to run without being logged in
sudo loginctl enable-linger pi

# Verify
loginctl show-user pi | grep Linger
# Should show: Linger=yes
```

### 3. Install Media Bridge

```bash
cd /home/pi
git clone https://github.com/yourusername/media-bridge.git
cd media-bridge

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install with D-Bus support
pip install -e .
pip install dbus-python
```

### 4. Configure Media Bridge

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

### 5. Test

```bash
# Run manually to test
media-bridge --config /etc/media-bridge/config.yaml -v
```

### 6. Install Service

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

### 7. Verify All Services

```bash
# Check all services are running
systemctl status spotifyd shairport-sync media-bridge

# Test Spotify: Open Spotify app, look for "Living Room" in devices
# Test AirPlay: Open iPhone/Mac, look for "Living Room" in AirPlay

# Watch media-bridge logs while playing audio
journalctl -u media-bridge -f
```

## Backup & Restore

### Files to Backup

Keep these files safe for easy re-provisioning:

```
/home/pi/.config/spotifyd/spotifyd.conf     # Spotify credentials & config
/etc/shairport-sync.conf                     # AirPlay config
/etc/media-bridge/config.yaml                # MQTT credentials & config
```

### Quick Backup Script

```bash
#!/bin/bash
# backup-media-bridge.sh
BACKUP_DIR="/home/pi/media-bridge-backup"
mkdir -p "$BACKUP_DIR"

cp ~/.config/spotifyd/spotifyd.conf "$BACKUP_DIR/"
sudo cp /etc/shairport-sync.conf "$BACKUP_DIR/"
sudo cp /etc/media-bridge/config.yaml "$BACKUP_DIR/"

# Create tarball with date
tar -czvf "media-bridge-backup-$(date +%Y%m%d).tar.gz" -C "$BACKUP_DIR" .
echo "Backup complete: media-bridge-backup-$(date +%Y%m%d).tar.gz"
```

### Quick Restore Script

After running the full installation steps above, restore your configs:

```bash
#!/bin/bash
# restore-media-bridge.sh
BACKUP_FILE="$1"

if [ -z "$BACKUP_FILE" ]; then
    echo "Usage: ./restore-media-bridge.sh media-bridge-backup-YYYYMMDD.tar.gz"
    exit 1
fi

# Extract backup
tar -xzvf "$BACKUP_FILE" -C /tmp/restore

# Restore configs
mkdir -p ~/.config/spotifyd
cp /tmp/restore/spotifyd.conf ~/.config/spotifyd/
sudo cp /tmp/restore/shairport-sync.conf /etc/
sudo mkdir -p /etc/media-bridge
sudo cp /tmp/restore/config.yaml /etc/media-bridge/

# Restart services
sudo systemctl restart spotifyd shairport-sync media-bridge

echo "Restore complete! Services restarted."
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

## Audio Output Configuration

### Find Your Audio Device

```bash
# List all ALSA playback devices
aplay -L

# List sound cards
cat /proc/asound/cards

# Test audio output
speaker-test -c 2 -t wav
```

### Common Audio Setups

#### USB DAC

```bash
# Find USB DAC device
aplay -l | grep -i usb

# Usually appears as "hw:1,0" or similar
# Update spotifyd.conf and shairport-sync.conf:
# device = "hw:1,0"
```

#### HDMI Audio

```bash
# Find HDMI device
aplay -l | grep -i hdmi

# Usually "hw:0,0" or "hw:1,0"
# May need to set in /boot/config.txt:
# hdmi_drive=2
```

#### Set Default Audio Device

Create/edit `~/.asoundrc` for user-level default:

```bash
cat > ~/.asoundrc << 'EOF'
defaults.pcm.card 1
defaults.ctl.card 1
EOF
```

Or system-wide in `/etc/asound.conf`.

### Volume Control

```bash
# List available mixer controls
amixer

# Set volume
amixer set Master 80%
amixer set PCM 80%

# For USB DAC, might be different name:
amixer -c 1 scontrols  # List controls on card 1
amixer -c 1 set Speaker 80%
```

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
# Check D-Bus session bus is available
echo $DBUS_SESSION_BUS_ADDRESS

# Should output something like:
# unix:path=/run/user/1000/bus

# If empty, ensure lingering is enabled:
sudo loginctl enable-linger pi

# For systemd services, ensure they have:
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus

# List available D-Bus services (run as pi user)
dbus-send --session --dest=org.freedesktop.DBus --type=method_call \
    --print-reply /org/freedesktop/DBus org.freedesktop.DBus.ListNames
```

### spotifyd Issues

```bash
# Check service status
systemctl status spotifyd
journalctl -u spotifyd -f

# Common issues:
# 1. Wrong credentials - verify username (not email) and password
# 2. Premium required - Spotify Connect needs Premium subscription
# 3. Audio device - verify device name with 'aplay -L'

# Test running manually
spotifyd --no-daemon --config-path ~/.config/spotifyd/spotifyd.conf -v

# Verify MPRIS is working (while music is playing)
dbus-send --session --dest=org.mpris.MediaPlayer2.spotifyd \
    --print-reply /org/mpris/MediaPlayer2 \
    org.freedesktop.DBus.Properties.Get \
    string:"org.mpris.MediaPlayer2.Player" string:"PlaybackStatus"
```

### shairport-sync Issues

```bash
# Check service status
systemctl status shairport-sync
journalctl -u shairport-sync -f

# Verify D-Bus is enabled in config
grep -A3 "dbus" /etc/shairport-sync.conf

# Test Avahi/mDNS is working
avahi-browse -a | grep AirPlay

# Verify MPRIS is working (while AirPlay is active)
dbus-send --session --dest=org.mpris.MediaPlayer2.ShairportSync \
    --print-reply /org/mpris/MediaPlayer2 \
    org.freedesktop.DBus.Properties.Get \
    string:"org.mpris.MediaPlayer2.Player" string:"PlaybackStatus"

# If device not appearing:
# - Check firewall allows ports 5000 (AirPlay) and 5353 (mDNS)
# - Ensure avahi-daemon is running: systemctl status avahi-daemon
```

### Media Bridge Not Detecting Playback

```bash
# Watch media-bridge logs
journalctl -u media-bridge -f

# Verify D-Bus MPRIS interfaces are available
# (run while audio is playing)
busctl --user list | grep -i mpris

# Should show entries like:
# org.mpris.MediaPlayer2.spotifyd
# org.mpris.MediaPlayer2.ShairportSync

# If not appearing, the audio services may not have D-Bus/MPRIS enabled
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

