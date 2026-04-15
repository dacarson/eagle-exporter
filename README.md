# eagle-exporter

Lightweight Python exporter for the Rainforest Automation Eagle local API.

It polls Eagle devices (with a focus on electric meters), normalizes values, and can either:

- print raw JSON snapshots to stdout, or
- publish flattened fields to InfluxDB.

## Features

- Collects and refreshes device inventory
- Polls electric meter devices by hardware address
- Flattens nested API data into InfluxDB-friendly scalar fields
- Skips unchanged meter writes using `last_contact`
- Publishes additional `wifi_status` and `devices` measurements

## Requirements

- Python 3
- An Eagle gateway with local API access
- Python API client: [`rfa-eagle-api` by tonymitchell](https://github.com/tonymitchell/rfa-eagle-api)
- Eagle hardware product page: [Rainforest Automation Eagle-3](https://rainforestautomation.com/us-retail-store/eagle-3-energy-gateway-and-smart-home-hub/)
- (Optional) InfluxDB (this project uses the `influxdb` Python client, typically for InfluxDB 1.x HTTP API)

Install Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Basic run (prints raw JSON only):

```bash
python3 eagle-exporter.py \
  --eagle_host 192.168.1.100 \
  --eagle_user YOUR_CLOUD_ID \
  --eagle_pass YOUR_INSTALLATION_ID \
  --raw
```

Publish to InfluxDB:

```bash
python3 eagle-exporter.py \
  --eagle_host 192.168.1.100 \
  --eagle_user YOUR_CLOUD_ID \
  --eagle_pass YOUR_INSTALLATION_ID \
  --influxdb \
  --influxdb_host 127.0.0.1 \
  --influxdb_port 8086 \
  --influxdb_db eagle
```

### Common arguments

- `--eagle_host` (required): Eagle local API host/IP
- `--eagle_user` (required): Eagle Cloud ID
- `--eagle_pass` (required): Eagle Installation ID
- `--raw`: print inventory/meter JSON payloads
- `--influxdb`: enable InfluxDB publishing
- `--influxdb_host` (default: `localhost`)
- `--influxdb_port` (default: `8086`)
- `--influxdb_user`
- `--influxdb_pass`
- `--influxdb_db` (default: `eagle`)
- `--meter_poll_interval` (default: `30` seconds)
- `--inventory_poll_interval` (default: `86400` seconds)
- `--eagle_timeout` (default: `30` seconds)
- `--verbose`: log Influx payloads and skip reasons
- `--debug`: enable verbose Eagle API debug logging

## Data model (InfluxDB)

The exporter writes:

- `wifi_status`: flattened current Eagle Wi-Fi status
- `devices`: one point per inventory device refresh
- `device_<hardware_address>`: meter variable fields for each electric meter

Fields are flattened and sanitized to scalar values. Non-scalar nested data is flattened using underscore-separated keys.

## Running as a systemd service

This repository includes `eagle-exporter.service` for Raspberry Pi-style deployment.

Example install:

```bash
sudo cp eagle-exporter.service /etc/systemd/system/eagle-exporter.service
sudoedit /etc/default/eagle-exporter
sudo systemctl daemon-reload
sudo systemctl enable --now eagle-exporter
```

Example `/etc/default/eagle-exporter`:

```bash
EAGLE_EXPORTER_ARGS="\
--eagle_host 192.168.1.100 \
--eagle_user YOUR_CLOUD_ID \
--eagle_pass YOUR_INSTALLATION_ID \
--influxdb \
--influxdb_host 127.0.0.1 \
--influxdb_port 8086 \
--influxdb_db eagle \
--meter_poll_interval 30"
```

Check logs:

```bash
journalctl -u eagle-exporter -f
```

## Notes

- Credentials are passed as CLI args in this project; prefer secure host practices and file permissions around environment files.
- If you only need local inspection/debugging, run with `--raw` and without `--influxdb`.
