"""
Configuration management for media bridge.

Supports:
- YAML config file
- Environment variable overrides
- Sensible defaults
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = [
    Path("/etc/media-bridge/config.yaml"),
    Path.home() / ".config" / "media-bridge" / "config.yaml",
    Path("config.yaml"),
]


@dataclass
class MQTTConfig:
    """MQTT broker configuration."""
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "media-bridge-living-room"
    topic_prefix: str = "media/living_room"
    keepalive: int = 60
    reconnect_delay: float = 5.0


@dataclass
class CECConfig:
    """HDMI-CEC and PulseAudio configuration."""
    enabled: bool = True
    device: str = "/dev/cec0"
    poll_interval: float = 5.0
    pulse_sink: str | None = None  # Auto-detect if None
    volume_step: int = 5  # Volume change per step (%)


@dataclass
class VolumeConfig:
    """Volume configuration for all sources."""
    # Master volume (0-100) - scales all output, default 50% for safety
    master: int = 50
    # Default volumes per source (0-100) - reset to this when source stops
    default_spotify: int = 15
    default_airplay: int = 15
    default_tv: int = 15
    # Whether to reset volume when source becomes inactive
    reset_on_stop: bool = True
    # Slew rate - kept for config compatibility but not used (instant changes)
    slew_rate: int = 0


@dataclass
class AudioConfig:
    """Audio stack configuration."""
    enabled: bool = True
    poll_interval: float = 1.0
    # TV audio source (S/PDIF)
    tv_alsa_device: str = "hw:CARD=ClearClick,DEV=0"
    # Output sink (auto-detect Scarlett if None)
    pulse_sink: str | None = None
    # Volume settings
    volume: VolumeConfig = field(default_factory=VolumeConfig)


@dataclass
class LogConfig:
    """Logging configuration."""
    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    file: str | None = None


@dataclass
class Config:
    """Main configuration container."""
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    cec: CECConfig = field(default_factory=CECConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    log: LogConfig = field(default_factory=LogConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Create Config from dictionary."""
        mqtt_data = data.get("mqtt", {})
        cec_data = data.get("cec", {})
        audio_data = data.get("audio", {})
        log_data = data.get("log", {})

        return cls(
            mqtt=MQTTConfig(
                host=mqtt_data.get("host", "localhost"),
                port=mqtt_data.get("port", 1883),
                username=mqtt_data.get("username"),
                password=mqtt_data.get("password"),
                client_id=mqtt_data.get("client_id", "media-bridge-living-room"),
                topic_prefix=mqtt_data.get("topic_prefix", "media/living_room"),
                keepalive=mqtt_data.get("keepalive", 60),
                reconnect_delay=mqtt_data.get("reconnect_delay", 5.0),
            ),
            cec=CECConfig(
                enabled=cec_data.get("enabled", True),
                device=cec_data.get("device", "/dev/cec0"),
                poll_interval=cec_data.get("poll_interval", 5.0),
                pulse_sink=cec_data.get("pulse_sink"),
                volume_step=cec_data.get("volume_step", 5),
            ),
            audio=AudioConfig(
                enabled=audio_data.get("enabled", True),
                poll_interval=audio_data.get("poll_interval", 1.0),
                tv_alsa_device=audio_data.get("tv_alsa_device", "hw:CARD=ClearClick,DEV=0"),
                pulse_sink=audio_data.get("pulse_sink"),
                volume=VolumeConfig(
                    master=audio_data.get("volume", {}).get("master", 100),
                    default_spotify=audio_data.get("volume", {}).get("default_spotify", 15),
                    default_airplay=audio_data.get("volume", {}).get("default_airplay", 15),
                    default_tv=audio_data.get("volume", {}).get("default_tv", 15),
                    reset_on_stop=audio_data.get("volume", {}).get("reset_on_stop", True),
                    slew_rate=audio_data.get("volume", {}).get("slew_rate", 25),
                ),
            ),
            log=LogConfig(
                level=log_data.get("level", "INFO"),
                format=log_data.get(
                    "format",
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
                ),
                file=log_data.get("file"),
            ),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> Config:
        """Load config from YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides."""
        # MQTT overrides
        if host := os.environ.get("MQTT_HOST"):
            self.mqtt.host = host
        if port := os.environ.get("MQTT_PORT"):
            self.mqtt.port = int(port)
        if username := os.environ.get("MQTT_USERNAME"):
            self.mqtt.username = username
        if password := os.environ.get("MQTT_PASSWORD"):
            self.mqtt.password = password
        if topic_prefix := os.environ.get("MQTT_TOPIC_PREFIX"):
            self.mqtt.topic_prefix = topic_prefix

        # CEC overrides
        if cec_enabled := os.environ.get("CEC_ENABLED"):
            self.cec.enabled = cec_enabled.lower() in ("true", "1", "yes")
        if cec_device := os.environ.get("CEC_DEVICE"):
            self.cec.device = cec_device

        # Audio overrides
        if audio_enabled := os.environ.get("AUDIO_ENABLED"):
            self.audio.enabled = audio_enabled.lower() in ("true", "1", "yes")

        # Log overrides
        if log_level := os.environ.get("LOG_LEVEL"):
            self.log.level = log_level.upper()


def load_config(config_path: str | Path | None = None) -> Config:
    """
    Load configuration from file with environment overrides.
    
    Args:
        config_path: Explicit path to config file. If None, searches default locations.
        
    Returns:
        Loaded and validated Config object.
    """
    config = Config()
    
    # Find config file
    if config_path:
        path = Path(config_path)
        if path.exists():
            logger.info(f"Loading config from {path}")
            config = Config.from_yaml(path)
        else:
            logger.warning(f"Config file not found: {path}, using defaults")
    else:
        # Search default locations
        for path in DEFAULT_CONFIG_PATHS:
            if path.exists():
                logger.info(f"Loading config from {path}")
                config = Config.from_yaml(path)
                break
        else:
            logger.info("No config file found, using defaults")
    
    # Apply environment overrides
    config.apply_env_overrides()
    
    return config


def setup_logging(config: LogConfig) -> None:
    """Configure logging based on config."""
    handlers: list[logging.Handler] = []
    
    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(config.format))
    handlers.append(console)
    
    # File handler if specified
    if config.file:
        file_handler = logging.FileHandler(config.file)
        file_handler.setFormatter(logging.Formatter(config.format))
        handlers.append(file_handler)
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, config.level.upper(), logging.INFO),
        format=config.format,
        handlers=handlers,
        force=True,
    )
    
    # Quiet down noisy libraries
    logging.getLogger("paho").setLevel(logging.WARNING)

