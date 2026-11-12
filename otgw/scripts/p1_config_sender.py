#!/usr/bin/env python3
"""
P1 Dongle Configuration Sender

Sends WiFi and MQTT configuration to P1 dongles via USB serial using the P1CONFIG protocol.
"""

import json
import serial
import time
from typing import Tuple


def send_p1_config(serial_port: str, config: dict, timeout: int = 2) -> Tuple[bool, str]:
    """Send WiFi/MQTT config to P1 dongle via serial.
    
    Args:
        serial_port: Path to serial device (e.g., '/dev/ttyACM0')
        config: Dictionary with config parameters
        timeout: Serial timeout in seconds
    
    Returns:
        (success: bool, message: str)
    """
    try:
        # Open serial connection at 115200 baud (USB CDC default)
        with serial.Serial(serial_port, 115200, timeout=timeout) as ser:
            # Build P1CONFIG command
            command = f"P1CONFIG:{json.dumps(config)}"
            
            # Send command with newline
            ser.write(command.encode() + b'\n')
            
            # Wait for response
            time.sleep(0.5)
            response = ser.readline().decode().strip()
            
            if response == "P1CONFIG:OK":
                return True, "Configuration sent successfully"
            elif response.startswith("P1CONFIG:ERROR:"):
                error_msg = response[15:]  # Extract error message
                return False, f"Configuration error: {error_msg}"
            elif response:
                return False, f"Unexpected response: {response}"
            else:
                return False, "No response from dongle"
                
    except serial.SerialException as e:
        return False, f"Serial communication error: {str(e)}"
    except json.JSONEncodeError as e:
        return False, f"Configuration encoding error: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def validate_p1_config(config: dict) -> Tuple[bool, str]:
    """Validate P1 configuration before sending.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        (valid: bool, message: str)
    """
    # Required field
    if 'wifi_ssid' not in config or not config['wifi_ssid']:
        return False, "WiFi SSID is required"
    
    # Validate SSID length
    if len(config['wifi_ssid']) > 32:
        return False, "WiFi SSID too long (max 32 characters)"
    
    # Validate password if provided
    if 'wifi_password' in config and config['wifi_password']:
        if len(config['wifi_password']) > 64:
            return False, "WiFi password too long (max 64 characters)"
    
    # Validate MQTT server if provided
    if 'mqtt_server' in config and config['mqtt_server']:
        if len(config['mqtt_server']) > 64:
            return False, "MQTT server too long (max 64 characters)"
    
    # Validate MQTT port if provided
    if 'mqtt_port' in config:
        try:
            port = int(config['mqtt_port'])
            if not (1 <= port <= 65535):
                return False, "MQTT port must be between 1 and 65535"
        except ValueError:
            return False, "MQTT port must be a valid number"
    
    # Validate MQTT topic if provided
    if 'mqtt_topic' in config and config['mqtt_topic']:
        if len(config['mqtt_topic']) > 128:
            return False, "MQTT topic too long (max 128 characters)"
    
    return True, "Configuration is valid"


if __name__ == "__main__":
    # Example usage
    import sys
    
    if len(sys.argv) != 2:
        print("Usage: python3 p1_config_sender.py /dev/ttyACM0")
        sys.exit(1)
    
    serial_port = sys.argv[1]
    
    # Example configuration
    config = {
        'wifi_ssid': 'my-network',
        'wifi_password': 'secret-password',
        'mqtt_server': 'broker.local',
        'mqtt_port': 1883,
        'mqtt_topic': 'home/energy/gas'
    }
    
    # Validate
    valid, val_msg = validate_p1_config(config)
    if not valid:
        print(f"Validation failed: {val_msg}")
        sys.exit(1)
    
    print(f"Sending configuration to {serial_port}...")
    
    # Send
    success, message = send_p1_config(serial_port, config)
    
    if success:
        print(f"✅ Success: {message}")
    else:
        print(f"❌ Error: {message}")
        sys.exit(1)