#!/usr/bin/with-contenv bashio
set -e

export SNAPSHOT_URL="$(bashio::config 'snapshot_url')"
export CALIBRATION_PATH="$(bashio::config 'calibration_path')"
export POLL_ACTIVE="$(bashio::config 'poll_active')"
export POLL_IDLE="$(bashio::config 'poll_idle')"
export PUBLISH_DEBUG_IMAGE="$(bashio::config 'publish_debug_image')"
export LOG_LEVEL="$(bashio::config 'log_level')"

if bashio::services.available "mqtt"; then
  export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
  export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
  export MQTT_USER="$(bashio::services 'mqtt' 'username')"
  export MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
else
  bashio::exit.nok "No MQTT service found. Install the Mosquitto broker add-on first."
fi

bashio::log.info "Starting DR7 panel reader"
exec python3 /decoder.py
