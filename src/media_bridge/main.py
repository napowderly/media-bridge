"""
Main entry point for the Media Bridge.

Orchestrates all components:
- CEC listener for TV power
- Audio mixer for per-source volume/mute/level (Spotify, AirPlay, TV)
- MQTT client for Home Assistant integration
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from typing import Any

from media_bridge.cec_listener import CECListener
from media_bridge.config import Config, load_config, setup_logging
from media_bridge.mixer import AudioMixer
from media_bridge.mqtt_client import MQTTClient, MQTTConfig

logger = logging.getLogger(__name__)


class MediaBridge:
    """
    Main orchestrator for the media bridge.
    
    Coordinates state between CEC, audio sources, and MQTT.
    """

    def __init__(self, config: Config):
        self.config = config
        self._running = False
        self._lock = threading.Lock()
        
        # Current combined state
        self._state: dict[str, Any] = {
            "tv_power": False,
            # Per-source state stored as nested dicts
            "sources": {},
        }
        
        # Initialize components
        self._mqtt: MQTTClient | None = None
        self._cec: CECListener | None = None
        self._mixer: AudioMixer | None = None
        
        # Rate limiting for volume commands (prevent feedback loops)
        self._last_volume_cmd: dict[str, float] = {}
        self._volume_cmd_cooldown = 0.2  # 200ms between commands per source

    def _on_cec_state_change(self, state_type: str, value: bool | int | str) -> None:
        """Handle CEC state changes (TV power only now)."""
        with self._lock:
            if state_type == "tv_power":
                self._state["tv_power"] = value
        
        # Publish to MQTT
        if self._mqtt:
            if state_type == "tv_power":
                self._mqtt.publish("tv/on", value)
        
        # Notify mixer of TV power state (enables/disables TV audio pipeline)
        if state_type == "tv_power" and self._mixer:
            self._mixer.set_tv_power(bool(value))

    def _on_cec_volume_command(self, command: str) -> None:
        """Handle CEC volume commands from TV remote - route to TV source."""
        if not self._mixer:
            return
        
        logger.info(f"CEC volume command: {command}")
        
        if command == "volume_up":
            self._mixer.tv_volume_up(step=self.config.cec.volume_step)
        elif command == "volume_down":
            self._mixer.tv_volume_down(step=self.config.cec.volume_step)
        elif command == "mute_toggle":
            self._mixer.tv_mute_toggle()

    def _on_source_state_change(self, source: str, key: str, value: Any) -> None:
        """Handle audio source state changes."""
        logger.debug(f"Source state change: {source}/{key} = {value}")
        
        with self._lock:
            if source not in self._state["sources"]:
                self._state["sources"][source] = {}
            self._state["sources"][source][key] = value
        
        # Publish to MQTT - per-source topics
        if self._mqtt:
            topic = f"source/{source}/{key}"
            self._mqtt.publish(topic, value)
            
            # Also publish active sources list
            if key == "state":
                active = self._mixer.get_active_sources() if self._mixer else []
                self._mqtt.publish("sources/active", active)

    def _on_mqtt_command(self, command: str, payload: str) -> None:
        """Handle MQTT commands."""
        logger.info(f"Processing command: {command}, payload: {payload}")
        
        try:
            # TV power control (via CEC)
            if command == "tv_power_on":
                if self._cec:
                    self._cec.power_on_tv()
            
            elif command == "tv_power_off":
                if self._cec:
                    self._cec.standby_tv()
            
            # Per-source volume commands (with rate limiting)
            elif command.startswith("source_") and command.endswith("_set_volume"):
                # e.g., source_spotify_set_volume
                source = command[7:-11]  # Extract source name
                volume = int(payload)
                
                # Rate limit to prevent feedback loops from HA slider
                now = time.time()
                last_cmd = self._last_volume_cmd.get(source, 0)
                if now - last_cmd < self._volume_cmd_cooldown:
                    logger.debug(f"Rate limiting volume command for {source}")
                    return
                self._last_volume_cmd[source] = now
                
                if self._mixer:
                    self._mixer.set_source_volume(source, volume)
            
            # Note: _set_mute must be checked BEFORE _mute (more specific first)
            elif command.startswith("source_") and command.endswith("_set_mute"):
                source = command[7:-9]
                muted = payload.lower() in ("true", "1", "yes", "on")
                if self._mixer:
                    self._mixer.set_source_mute(source, muted)
            
            elif command.startswith("source_") and command.endswith("_mute"):
                source = command[7:-5]
                if self._mixer:
                    self._mixer.toggle_source_mute(source)
            
            # Playback control
            elif command.startswith("source_") and command.endswith("_play"):
                source = command[7:-5]
                if self._mixer:
                    self._mixer.source_play(source)
            
            elif command.startswith("source_") and command.endswith("_pause"):
                source = command[7:-6]
                if self._mixer:
                    self._mixer.source_pause(source)
            
            elif command.startswith("source_") and command.endswith("_stop"):
                source = command[7:-5]
                if self._mixer:
                    self._mixer.source_stop(source)
            
            # TV silence detection settings
            elif command == "tv_set_silence_threshold":
                threshold = int(payload)
                if self._mixer:
                    self._mixer.set_tv_silence_threshold(threshold)
            
            elif command == "tv_set_silence_duration":
                duration = float(payload)
                if self._mixer:
                    self._mixer.set_tv_silence_duration(duration)
            
            elif command == "tv_set_auto_mute":
                enabled = payload.lower() in ("true", "1", "yes", "on")
                if self._mixer:
                    self._mixer.set_tv_auto_mute(enabled)
            
            # Master volume control
            elif command == "set_master_volume":
                volume = int(payload)
                if self._mixer:
                    self._mixer.set_master_volume(volume)
            
            # Default volume settings
            elif command.startswith("source_") and command.endswith("_set_default_volume"):
                source = command[7:-19]  # Extract source name
                volume = int(payload)
                if self._mixer:
                    self._mixer.set_default_volume(source, volume)
            
            elif command == "set_reset_on_stop":
                enabled = payload.lower() in ("true", "1", "yes", "on")
                if self._mixer:
                    self._mixer.set_reset_on_stop(enabled)
            
            elif command == "set_slew_rate":
                rate = int(payload)
                if self._mixer:
                    self._mixer.set_slew_rate(rate)
            
            else:
                logger.warning(f"Unknown command: {command}")
                
        except ValueError as e:
            logger.error(f"Invalid command payload: {e}")
        except Exception as e:
            logger.error(f"Error processing command {command}: {e}")

    def start(self) -> None:
        """Start all components."""
        if self._running:
            return
        
        self._running = True
        logger.info("Starting Media Bridge...")
        
        # Start MQTT client
        mqtt_config = MQTTConfig(
            host=self.config.mqtt.host,
            port=self.config.mqtt.port,
            username=self.config.mqtt.username,
            password=self.config.mqtt.password,
            client_id=self.config.mqtt.client_id,
            topic_prefix=self.config.mqtt.topic_prefix,
            keepalive=self.config.mqtt.keepalive,
            reconnect_delay=self.config.mqtt.reconnect_delay,
        )
        self._mqtt = MQTTClient(mqtt_config, on_command=self._on_mqtt_command)
        self._mqtt.start()
        
        # Wait for MQTT connection
        for _ in range(50):  # 5 second timeout
            if self._mqtt.connected:
                break
            time.sleep(0.1)
        
        if not self._mqtt.connected:
            logger.warning("MQTT not connected after timeout, continuing anyway")
        
        # Start CEC listener (TV power + volume commands to mixer)
        if self.config.cec.enabled:
            self._cec = CECListener(
                device=self.config.cec.device,
                on_state_change=self._on_cec_state_change,
                on_volume_command=self._on_cec_volume_command,
                poll_interval=self.config.cec.poll_interval,
                volume_step=self.config.cec.volume_step,
            )
            self._cec.start()
        else:
            logger.info("CEC disabled in config")
        
        # Start audio mixer (handles all sources)
        if self.config.audio.enabled:
            vol_cfg = self.config.audio.volume
            self._mixer = AudioMixer(
                on_state_change=self._on_source_state_change,
                tv_alsa_device=self.config.audio.tv_alsa_device,
                pulse_sink=self.config.audio.pulse_sink,
                master_volume=vol_cfg.master,
                default_volumes={
                    "spotify": vol_cfg.default_spotify,
                    "airplay": vol_cfg.default_airplay,
                    "tv": vol_cfg.default_tv,
                },
                reset_on_stop=vol_cfg.reset_on_stop,
                slew_rate=vol_cfg.slew_rate,
            )
            self._mixer.start()
        else:
            logger.info("Audio mixer disabled in config")
        
        # Publish initial state after short delay
        time.sleep(1.0)
        self._publish_full_state()
        
        logger.info("Media Bridge started successfully")

    def stop(self) -> None:
        """Stop all components gracefully."""
        if not self._running:
            return
        
        self._running = False
        logger.info("Stopping Media Bridge...")
        
        # Stop in reverse order
        if self._mixer:
            self._mixer.stop()
            self._mixer = None
        
        if self._cec:
            self._cec.stop()
            self._cec = None
        
        if self._mqtt:
            self._mqtt.stop()
            self._mqtt = None
        
        logger.info("Media Bridge stopped")

    def _publish_full_state(self) -> None:
        """Publish complete state to MQTT."""
        if not self._mqtt:
            return
        
        # Clear dedup cache
        self._mqtt._last_state.clear()
        
        # Publish TV power
        with self._lock:
            tv_power = self._state.get("tv_power", False)
        self._mqtt.publish("tv/on", tv_power)
        
        # Publish all source states
        if self._mixer:
            states = self._mixer.get_all_states()
            for source_name, source_state in states.items():
                for key, value in source_state.items():
                    if key != "name":  # Skip the name field
                        self._mqtt.publish(f"source/{source_name}/{key}", value)
            
            # Publish active sources
            active = self._mixer.get_active_sources()
            self._mqtt.publish("sources/active", active)
            
            # Publish TV silence detection settings
            self._mqtt.publish("source/tv/silence_threshold", self._mixer.get_tv_silence_threshold())
            self._mqtt.publish("source/tv/silence_duration", self._mixer.get_tv_silence_duration())
            self._mqtt.publish("source/tv/auto_mute", self._mixer.get_tv_auto_mute())
            self._mqtt.publish("source/tv/level_db", round(self._mixer.get_tv_level_db(), 1))
            
            # Publish master volume and settings
            self._mqtt.publish("master/volume", self._mixer.get_master_volume())
            self._mqtt.publish("master/reset_on_stop", self._mixer.get_reset_on_stop())
            self._mqtt.publish("master/slew_rate", self._mixer.get_slew_rate())
            
            # Publish default volumes
            for source in ["spotify", "airplay", "tv"]:
                self._mqtt.publish(f"source/{source}/default_volume", self._mixer.get_default_volume(source))
        
        logger.info("Published full state")

    def run_forever(self) -> None:
        """Run until interrupted."""
        self.start()
        
        try:
            while self._running:
                time.sleep(1.0)
                
                # Periodic health check / reconnect logic could go here
                if self._mqtt and not self._mqtt.connected:
                    logger.warning("MQTT disconnected, will auto-reconnect")
                    
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            self.stop()


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="RPI Living Room Media Bridge - MQTT bridge for Home Assistant"
    )
    parser.add_argument(
        "-c", "--config",
        type=str,
        help="Path to config file (default: search standard locations)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show version and exit",
    )
    
    args = parser.parse_args()
    
    if args.version:
        from media_bridge import __version__
        print(f"media-bridge {__version__}")
        return 0
    
    # Load config
    config = load_config(args.config)
    
    # Override log level if verbose
    if args.verbose:
        config.log.level = "DEBUG"
    
    # Setup logging
    setup_logging(config.log)
    
    logger.info("Media Bridge starting...")
    logger.debug(f"Config: MQTT={config.mqtt.host}:{config.mqtt.port}, "
                 f"CEC={config.cec.enabled}, Audio={config.audio.enabled}")
    
    # Create and run bridge
    bridge = MediaBridge(config)
    
    # Setup signal handlers
    def handle_signal(signum: int, frame: Any) -> None:
        logger.info(f"Received signal {signum}, shutting down...")
        bridge.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    try:
        bridge.run_forever()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
