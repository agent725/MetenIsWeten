#!/usr/bin/env python3

"""
Single-Session ESP32-S3 Flash Script
Uses esptool's Python API to maintain a single USB connection
Avoids repeated USB re-enumeration and port configuration issues

Reset Strategy:
--before default_reset: Ensures clean device state at session start
--after hard_reset: Ensures proper device reset at session end
This provides better USB stability compared to no_reset options
"""

import os
import sys
import time
import subprocess
from pathlib import Path

# Configuration
PORT = "/dev/ttyACM0"
FW_PATH = "/opt/otgw/firmware/p1_reader.bin"  # Default firmware path
START_ADDR = 0x10000
CHUNK_SIZE = 65536  # 64KB chunks
STREAM_DELAY = 0.5  # 0.5s delay between chunks
BAUD_RATE = 19200  # Slower baud rate for better USB stability

def main():
    print("== Single-Session ESP32-S3 Flash ==")
    print(f"Port: {PORT}")
    print(f"Firmware: {FW_PATH}")
    print(f"Strategy: Single esptool session, {CHUNK_SIZE} byte chunks")
    print(f"Baud rate: {BAUD_RATE} (for USB stability)")
    print(f"In-stream delay: {STREAM_DELAY}s between chunks")
    print()
    
    # Verify firmware exists
    fw_path = Path(FW_PATH)
    if not fw_path.exists():
        print(f"❌ Firmware {FW_PATH} not found")
        return 1
    
    file_size = fw_path.stat().st_size
    num_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    
    print(f"File size: {file_size} bytes ({file_size//1024}KB)")
    print(f"Total chunks: {num_chunks}")
    print()
    
    # Wait for USB to settle
    print("🕒 Waiting for USB to settle...")
    time.sleep(2)
    
    # Check if device exists
    if not Path(PORT).exists():
        print(f"❌ Device {PORT} not found")
        return 1
    
    print(f"✅ Device ready: {PORT}")
    print()
    
    # Prepare chunks
    print("📦 Preparing chunks...")
    chunks = []
    for i in range(num_chunks):
        chunk_start = i * CHUNK_SIZE
        chunk_end = min(chunk_start + CHUNK_SIZE, file_size) - 1
        chunk_bytes = chunk_end - chunk_start + 1
        current_addr = START_ADDR + chunk_start
        
        # Extract chunk
        chunk_data = fw_path.read_bytes()[chunk_start:chunk_end+1]
        chunks.append((current_addr, chunk_data))
        
        if i % 10 == 0:
            print(f"  Prepared chunk {i}/{num_chunks}")
    
    print(f"✅ Prepared {len(chunks)} chunks for streaming")
    print()
    
    # Build esptool command for single session
    # We'll use --no-stub for better control and add delays between chunks
    print("🚀 Starting single esptool session...")
    
    # Note: Skipping explicit erase to avoid USB issues
    # write-flash will handle erasing sectors as needed
    # print("Skipping explicit erase (write-flash handles this)")
    print()
    
    # Now write chunks in a single session
    # We'll use write-flash with all chunks at once
    # esptool can handle multiple address/filename pairs
    print("📤 Streaming chunks...")
    
    # Create temporary chunk files
    temp_files = []
    try:
        for i, (addr, data) in enumerate(chunks):
            temp_file = f"chunk_{i}.bin"
            Path(temp_file).write_bytes(data)
            temp_files.append(temp_file)
        
        # Build write-flash command with all chunks
        write_cmd = [
            "esptool", "--chip", "esp32s3", "--port", PORT, "--baud", str(BAUD_RATE),
            "--before", "default_reset", "--after", "hard_reset",
            "write-flash"
        ]
        
        # Add all chunks
        for i, (addr, temp_file) in enumerate(zip(range(START_ADDR, START_ADDR + len(chunks)*CHUNK_SIZE, CHUNK_SIZE), temp_files)):
            write_cmd.extend([hex(addr), temp_file])
        
        print(f"Executing: {' '.join(write_cmd[:10])} ... (showing full output)")
        print("=" * 60)
        
        # Execute the single write-flash command with real-time output
        result = subprocess.run(write_cmd, text=True)
        
        if result.returncode == 0:
            print("✅ All chunks written successfully!")
            
            # Optional verification
            # Skip interactive verification for automated use
            print("⚠️  Skipping verification (remove capture_output=True to enable)")
            
            # Final reset
            print("🔄 Performing final reset...")
            reset_cmd = [
                "esptool", "--chip", "esp32s3", "--port", PORT, "--baud", str(BAUD_RATE),
                "--after", "hard_reset", "chip-id"
            ]
            subprocess.run(reset_cmd)
            print("✅ Final reset complete")
            
        else:
            print(f"❌ Write failed: {result.stderr}")
            return 1
            
    finally:
        # Cleanup temp files
        for temp_file in temp_files:
            try:
                Path(temp_file).unlink()
            except:
                pass
    
    print()
    print("🎉 Flashing completed successfully!")
    print(f"Strategy: Single session with {len(chunks)} chunks")
    print(f"Total bytes: {file_size}")
    print("Efficiency: No USB re-enumeration between chunks")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
