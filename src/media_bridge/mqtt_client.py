"""
MQTT client for Home Assistant integration.

Handles:
- Publishing per-source state (volume, mute, level, playback state)
- Subscribing to per-source command topics
- Last Will and Testament for availability
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


@dataclass
class MQTTConfig:
    """MQTT connection configuration."""
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "media-bridge-living-room"
    topic_prefix: str = "media/living_room"
    keepalive: int = 60
    reconnect_delay: float = 5.0


# Sources we support
SOURCES = ["spotify", "airplay", "tv"]

# Command topic suffixes and their handlers
# Per-source commands are handled dynamically
COMMAND_TOPICS = {
    # TV power
    "tv/power_on": "tv_power_on",
    "tv/power_off": "tv_power_off",
    # TV silence detection settings
    "source/tv/set_silence_threshold": "tv_set_silence_threshold",
    "source/tv/set_silence_duration": "tv_set_silence_duration",
    "source/tv/set_auto_mute": "tv_set_auto_mute",
    # Master volume control
    "master/set_volume": "set_master_volume",
    "master/set_reset_on_stop": "set_reset_on_stop",
    "master/set_slew_rate": "set_slew_rate",
}

# Add per-source command topics
for source in SOURCES:
    COMMAND_TOPICS[f"source/{source}/set_volume"] = f"source_{source}_set_volume"
    COMMAND_TOPICS[f"source/{source}/mute"] = f"source_{source}_mute"
    COMMAND_TOPICS[f"source/{source}/set_mute"] = f"source_{source}_set_mute"
    COMMAND_TOPICS[f"source/{source}/play"] = f"source_{source}_play"
    COMMAND_TOPICS[f"source/{source}/pause"] = f"source_{source}_pause"
    COMMAND_TOPICS[f"source/{source}/stop"] = f"source_{source}_stop"
    COMMAND_TOPICS[f"source/{source}/set_default_volume"] = f"source_{source}_set_default_volume"


class MQTTClient:
    """
    MQTT client for media bridge.
    
    Publishes state changes and subscribes to command topics.
    Implements LWT for availability tracking.
    """

    def __init__(
        self,
        config: MQTTConfig,
        on_command: Callable[[str, str], None] | None = None,
    ):
        self.config = config
        self.on_command = on_command
        
        self._client: mqtt.Client | None = None
        self._connected = False
        self._lock = threading.Lock()
        self._running = False
        
        # Track last published state to avoid duplicates
        self._last_state: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def _topic(self, suffix: str) -> str:
        """Build full topic from suffix."""
        return f"{self.config.topic_prefix}/{suffix}"

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: any,
        flags: dict,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None = None,
    ) -> None:
        """Handle MQTT connection."""
        if reason_code == 0:
            logger.info(f"Connected to MQTT broker at {self.config.host}:{self.config.port}")
            with self._lock:
                self._connected = True
            
            # Publish online status
            self._publish_availability(True)
            
            # Subscribe to command topics
            for topic_suffix in COMMAND_TOPICS:
                topic = self._topic(topic_suffix)
                client.subscribe(topic, qos=0)
                logger.debug(f"Subscribed to {topic}")
        else:
            logger.error(f"MQTT connection failed: {reason_code}")
            with self._lock:
                self._connected = False

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: any,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None = None,
    ) -> None:
        """Handle MQTT disconnection."""
        logger.warning(f"Disconnected from MQTT broker: {reason_code}")
        with self._lock:
            self._connected = False

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: any,
        message: mqtt.MQTTMessage,
    ) -> None:
        """Handle incoming MQTT messages."""
        try:
            topic = message.topic
            payload = message.payload.decode("utf-8") if message.payload else ""
            
            # Extract the topic suffix
            prefix = self.config.topic_prefix + "/"
            if topic.startswith(prefix):
                suffix = topic[len(prefix):]
                
                if suffix in COMMAND_TOPICS:
                    command = COMMAND_TOPICS[suffix]
                    logger.info(f"Received command: {command} with payload: {payload}")
                    
                    if self.on_command:
                        self.on_command(command, payload)
                else:
                    logger.debug(f"Unknown topic suffix: {suffix}")
            else:
                logger.debug(f"Message on unexpected topic: {topic}")
                
        except Exception as e:
            logger.error(f"Error handling MQTT message: {e}")

    def _publish_availability(self, online: bool) -> None:
        """Publish availability status."""
        self.publish("availability", "online" if online else "offline", retain=True)

    def start(self) -> None:
        """Start the MQTT client."""
        if self._running:
            return
        
        self._running = True
        
        # Create MQTT client with version 5
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self.config.client_id,
            protocol=mqtt.MQTTv5,
        )
        
        # Set callbacks
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        
        # Set credentials if provided
        if self.config.username:
            self._client.username_pw_set(self.config.username, self.config.password)
        
        # Set Last Will and Testament
        lwt_topic = self._topic("availability")
        self._client.will_set(lwt_topic, "offline", qos=1, retain=True)
        
        # Connect
        try:
            self._client.connect(
                self.config.host,
                self.config.port,
                keepalive=self.config.keepalive,
            )
            self._client.loop_start()
            logger.info("MQTT client started")
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            self._running = False

    def stop(self) -> None:
        """Stop the MQTT client gracefully."""
        if not self._running:
            return
        
        self._running = False
        
        if self._client:
            # Publish offline status before disconnecting
            self._publish_availability(False)
            time.sleep(0.1)  # Brief delay to ensure message is sent
            
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        
        with self._lock:
            self._connected = False
        
        logger.info("MQTT client stopped")

    def publish(
        self,
        topic_suffix: str,
        payload: str | int | bool | dict | list,
        retain: bool = True,
        qos: int = 1,
    ) -> bool:
        """
        Publish a message to MQTT.
        
        Args:
            topic_suffix: Topic suffix (appended to prefix)
            payload: Message payload
            retain: Whether to retain the message
            qos: Quality of service level
            
        Returns:
            True if published successfully
        """
        if not self._client or not self.connected:
            logger.warning(f"Cannot publish, not connected to MQTT")
            return False
        
        topic = self._topic(topic_suffix)
        
        # Convert payload to string
        if isinstance(payload, bool):
            payload_str = "true" if payload else "false"
        elif isinstance(payload, (dict, list)):
            payload_str = json.dumps(payload)
        else:
            payload_str = str(payload)
        
        # Skip if unchanged (for state topics)
        if retain and topic in self._last_state:
            if self._last_state[topic] == payload_str:
                return True
        
        try:
            result = self._client.publish(topic, payload_str, qos=qos, retain=retain)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self._last_state[topic] = payload_str
                logger.debug(f"Published {topic}: {payload_str}")
                return True
            else:
                logger.error(f"Failed to publish to {topic}: {result.rc}")
                return False
        except Exception as e:
            logger.error(f"Error publishing to {topic}: {e}")
            return False
