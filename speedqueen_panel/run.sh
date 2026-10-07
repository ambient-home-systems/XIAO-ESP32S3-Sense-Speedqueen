#!/usr/bin/with-contenv bashio
set -e

# The machines list is handed to the decoder as JSON rather than unpacked into
# environment variables — there can be any number of them.
# bashio prints a list of objects one JSON object per line, not as a JSON array;
# jq -s gathers them into one array (one machine or many).
export MACHINES="$(bashio::config 'machines' | jq -sc '.')"
export PUBLISH_DEBUG_IMAGE="$(bashio::config 'publish_debug_image')"
export LOG_LEVEL="$(bashio::config 'log_level')"

if ! bashio::config.has_value 'machines'; then
  bashio::exit.nok "No machines configured. Add at least one machine with a type, snapshot_url and calibration_path."
fi

if bashio::services.available "mqtt"; then
  export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
  export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
  export MQTT_USER="$(bashio::services 'mqtt' 'username')"
  export MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
else
  bashio::exit.nok "No MQTT service found. Install the Mosquitto broker add-on first."
fi

bashio::log.info "Starting Speed Queen panel reader"
exec python3 /decoder.py
