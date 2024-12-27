import os
from dotenv import load_dotenv
import requests
import json
import threading
import paho.mqtt.client as mqtt
import time
from datetime import datetime, timedelta

# Load environment variables from .env file
load_dotenv()

# MQTT Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_BASE_TOPIC = os.getenv("MQTT_BASE_TOPIC", "homeassistant")

# API URL and headers
url = os.getenv("API_URL", "https://global.solaxcloud.com/api/v2/dataAccess/realtimeInfo/get")
headers = {
    'tokenId': os.getenv("API_TOKEN_ID", ""),
    'Content-Type': 'application/json'
}

# List of serial numbers from environment variables (comma-separated)
serial_numbers = os.getenv("SERIAL_NUMBERS", "").split(",")

# Healthchecks.io ping URL
HEALTHCHECKS_URL = os.getenv("HEALTHCHECKS_URL", "")

# Shared totals for all inverters (do not update these in the threads directly)
totals = {
    "acpower": 0.0,
    "yieldtoday": 0.0,
    "yieldtotal": 0.0,
}

# Dictionary to keep track of each inverter's fetch results
fetch_results_lock = threading.Lock()
fetch_results = {}


# MQTT client setup
mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
mqtt_client.loop_start()  # Start the loop in a separate thread

def on_disconnect(client, userdata, rc):
    if rc != 0:
        print("Unexpected disconnection. Reconnecting...")
        client.reconnect()

mqtt_client.on_disconnect = on_disconnect

def ping_healthcheck(success=True):
    """
    Send a success or failure ping to Healthchecks.io if URL is provided.
    """
    if not HEALTHCHECKS_URL:  # Check if the URL is empty
        print("Healthchecks.io URL is not provided. Skipping healthcheck ping.")
        return

    try:
        if success:
            # Successful run
            requests.get(HEALTHCHECKS_URL, timeout=5)
        else:
            # Failed run
            requests.get(f"{HEALTHCHECKS_URL}/fail", timeout=5)
    except requests.RequestException as e:
        print(f"Healthcheck ping failed: {e}")


# Function to publish MQTT discovery messages
def publish_discovery(serial_number, key, name, unit, device_class=None, state_class=None, global_sensor=False):
    """
    Publishes MQTT discovery messages for both individual and total sensors.
    """
    if global_sensor:
        # Discovery topic for the aggregated sensor
        topic = f"{MQTT_BASE_TOPIC}/sensor/solax_totals_{key}/config"
        name = f"Total {name}"
        unique_id = f"solax_totals_{key}"
        state_topic = f"{MQTT_BASE_TOPIC}/sensor/solax_totals/{key}"
        device_identifiers = ["solax_totals"]
        device_name = "Solax Totals"
    else:
        # Discovery topic for the individual inverter sensor
        topic = f"{MQTT_BASE_TOPIC}/sensor/solax_{serial_number}_{key}/config"
        state_topic = f"{MQTT_BASE_TOPIC}/sensor/solax/{serial_number}/{key}"
        unique_id = f"solax_{serial_number}_{key}"
        device_identifiers = [f"solax_{serial_number}"]
        device_name = f"Solax Inverter {serial_number}"

    payload = {
        "name": name,
        "state_topic": state_topic,
        "unique_id": unique_id,
        "device": {
            "identifiers": device_identifiers,
            "name": device_name,
            "manufacturer": "Solax",
        },
        "unit_of_measurement": unit,
    }
    
    # Add required attributes for Energy Dashboard sensors
    if device_class:
        payload["device_class"] = device_class
    if state_class:
        payload["state_class"] = state_class

    mqtt_client.publish(topic, json.dumps(payload), retain=True)

# Function to fetch data and publish to MQTT for a single inverter
def fetch_and_publish(serial_number):
    """
    Fetch data for a single serial_number, publish its individual data,
    and store success/failure plus numeric values in fetch_results.
    """
    payload = json.dumps({"wifiSn": serial_number})
    local_result = {
        "success": False,
        "acpower": 0.0,
        "yieldtoday": 0.0,
        "yieldtotal": 0.0,
        "powerdc1": 0.0,
        "powerdc2": 0.0,
    }

    try:
        # Fetch data from API
        response = requests.get(url, headers=headers, data=payload)
        parsed_response = json.loads(response.text)

        # Ensure 'result' exists and is not None
        result = parsed_response.get("result")
        if result is not None:
            # Convert values to floats (default to 0.0 if None)
            ac_power = float(result.get("acpower", 0.0))
            yield_today = float(result.get("yieldtoday", 0.0))
            yield_total = float(result.get("yieldtotal", 0.0))
            powerdc1 = float(result.get("powerdc1", 0.0))
            powerdc2 = float(result.get("powerdc2", 0.0))
            uploadTime = result.get("uploadTime", "")

            # Publish discovery topics for this inverter
            publish_discovery(serial_number, "acpower", "AC Power", "W", "power")
            publish_discovery(serial_number, "powerdc1", "Panel 1 DC Power", "W", "power")
            publish_discovery(serial_number, "powerdc2", "Panel 2 DC Power", "W", "power")
            publish_discovery(serial_number, "yieldtoday", "Yield Today", "kWh", "energy", "total_increasing")
            publish_discovery(serial_number, "yieldtotal", "Yield Total", "kWh", "energy", "total_increasing")

            # Publish sensor data
            mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax/{serial_number}/acpower", ac_power, retain=True)
            mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax/{serial_number}/powerdc1", powerdc1, retain=True)
            mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax/{serial_number}/powerdc2", powerdc2, retain=True)
            mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax/{serial_number}/yieldtoday", yield_today, retain=True)
            mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax/{serial_number}/yieldtotal", yield_total, retain=True)

            # Mark success and store data locally
            local_result["success"] = True
            local_result["acpower"] = ac_power
            local_result["yieldtoday"] = yield_today
            local_result["yieldtotal"] = yield_total
            local_result["powerdc1"] = powerdc1
            local_result["powerdc2"] = powerdc2

            print(f"Published data for {serial_number}: "
                  f"AC Power={ac_power}, Yield Today={yield_today}, DC Power 1={powerdc1}, DC Power 2={powerdc2}, "
                  f"Yield Total={yield_total}, Upload Time={uploadTime}")
        else:
            # If the response doesn't have "result", print the error
            print(response.text)
            print(f"Serial Number: {serial_number}, Error: 'result' is None or missing in the response")

    except Exception as e:
        print(f"Serial Number: {serial_number}, Error: {e}")

    # Update the shared fetch_results dict (thread-safe)
    with fetch_results_lock:
        fetch_results[serial_number] = local_result

def run_fetch_cycle():
    """
    Runs one full fetch cycle for all inverters,
    checks successes, publishes totals if all successful,
    and pings healthchecks accordingly.
    """
    # Clear out any old results
    with fetch_results_lock:
        fetch_results.clear()

    # Create and start threads for each serial number
    threads = []
    for sn in serial_numbers:
        thread = threading.Thread(target=fetch_and_publish, args=(sn,))
        threads.append(thread)
        thread.start()

    # Wait for all threads to complete
    for thread in threads:
        thread.join()

    # ----- Check results and update totals if ALL requests succeeded -----
    all_success = True
    with fetch_results_lock:
        for sn in serial_numbers:
            result = fetch_results.get(sn)
            if not result or not result["success"]:
                all_success = False
                break

    if all_success:
        print("All inverters fetched successfully. Updating totals...")

        # Re-initialize totals to 0.0 for fresh summation
        totals["acpower"] = 0.0
        totals["yieldtoday"] = 0.0
        totals["yieldtotal"] = 0.0

        # Sum the values from all inverters
        with fetch_results_lock:
            for sn in serial_numbers:
                inverter_data = fetch_results[sn]
                totals["acpower"] += inverter_data["acpower"]
                totals["yieldtoday"] += inverter_data["yieldtoday"]
                totals["yieldtotal"] += inverter_data["yieldtotal"]

        # Publish discovery for totals
        publish_discovery("", "acpower", "AC Power", "W", "power", global_sensor=True)
        publish_discovery("", "yieldtoday", "Yield Today", "kWh", "energy", "total_increasing", global_sensor=True)
        publish_discovery("", "yieldtotal", "Yield Total", "kWh", "energy", "total_increasing", global_sensor=True)

        # Publish totals to MQTT
        mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax_totals/acpower", totals["acpower"], retain=True)
        mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax_totals/yieldtoday", totals["yieldtoday"], retain=True)
        mqtt_client.publish(f"{MQTT_BASE_TOPIC}/sensor/solax_totals/yieldtotal", totals["yieldtotal"], retain=True)

        print("Published totals to MQTT.")
        print(totals)

        # Healthchecks.io ping: success
        ping_healthcheck(True)
    else:
        # If ANY inverter failed, skip totals update
        print("One or more inverters failed. Skipping totals update this cycle.")

        # Healthchecks.io ping: fail
        ping_healthcheck(False)

def wait_for_next_5m_plus_10s():
    """
    Sleep until the next clock time that is:
       minutes = multiple of 5
       seconds = 10
    Examples: hh:05:10, hh:10:10, hh:15:10, ...
    """
    now = datetime.now()

    # Current minute block (0-4, 5-9, 10-14, etc.)
    # We'll set our target minute to the next multiple of 5 from now.
    # But if we are already past "xx:xx:10" within that block, we move to the next block.
    this_block_start = (now.minute // 5) * 5  # e.g., if now.minute=12, block_start=10
    target_minute = this_block_start
    target_second = 10

    # Construct a tentative next run time with the current hour/day/etc.
    # We'll adjust below if we are already "too late".
    target_time = datetime(
        year=now.year,
        month=now.month,
        day=now.day,
        hour=now.hour,
        minute=target_minute,
        second=target_second
    )

    # If we haven't passed "xx:target_minute:10" yet in this 5-minute block,
    # but are within the same block, we might be good. Otherwise, add 5 minutes.
    if target_time <= now:
        # Move to the next 5-minute block
        target_time += timedelta(minutes=5)

    # Sleep until target_time
    sleep_seconds = (target_time - datetime.now()).total_seconds()
    print(f"Next run scheduled at {target_time.strftime('%H:%M:%S')} (sleeping {int(sleep_seconds)}s).")
    if sleep_seconds > 0:
        time.sleep(sleep_seconds)

if __name__ == "__main__":
    try:
        # 1) Run immediately
        run_fetch_cycle()

        # 2) Then loop forever, waiting until next 5-minute boundary + 10 seconds each time
        while True:
            wait_for_next_5m_plus_10s()
            run_fetch_cycle()

    except KeyboardInterrupt:
        print("Stopping script...")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
