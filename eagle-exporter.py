#!/usr/bin/python3

import argparse
import json
import re
import time
from collections.abc import Mapping, Sequence

import eagle  # from rfa-eagle-api


def configure_eagle_timeout(timeout_seconds):
    """Override Eagle localapi request timeout."""
    original_post = eagle.localapi.requests.post

    def post_with_timeout(*args, **kwargs):
        kwargs["timeout"] = timeout_seconds
        return original_post(*args, **kwargs)

    eagle.localapi.requests.post = post_with_timeout


def object_to_data(value):
    """Best-effort conversion to JSON-serializable Python data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): object_to_data(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [object_to_data(v) for v in value]

    for method_name in ("to_json", "as_json", "json"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                raw = method()
                if isinstance(raw, str):
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError:
                        return raw
                return object_to_data(raw)
            except Exception:
                pass

    if hasattr(value, "__dict__"):
        data = {}
        for k, v in vars(value).items():
            if callable(v) or (k.startswith("__") and k.endswith("__")):
                continue
            key = k[1:] if k.startswith("_") else k
            data[str(key)] = object_to_data(v)
        return data

    return str(value)


def flatten_for_influx(data, prefix=""):
    """Flatten nested dict/list data to scalar Influx fields."""
    fields = {}
    if isinstance(data, Mapping):
        for key, value in data.items():
            sub_prefix = f"{prefix}_{key}" if prefix else str(key)
            fields.update(flatten_for_influx(value, sub_prefix))
        return fields

    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        for idx, value in enumerate(data):
            sub_prefix = f"{prefix}_{idx}" if prefix else str(idx)
            fields.update(flatten_for_influx(value, sub_prefix))
        return fields

    if data is None:
        return fields

    if isinstance(data, (bool, int, float, str)):
        if prefix:
            fields[prefix] = data
        return fields

    if prefix:
        fields[prefix] = str(data)
    return fields


def drop_null_value_objects(data):
    """Recursively drop dict objects that contain value=None."""
    if isinstance(data, Mapping):
        if data.get("value") is None and "value" in data:
            return None, True

        cleaned = {}
        for key, value in data.items():
            cleaned_value, dropped = drop_null_value_objects(value)
            if not dropped:
                cleaned[key] = cleaned_value
        return cleaned, False

    if isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        cleaned = []
        for item in data:
            cleaned_item, dropped = drop_null_value_objects(item)
            if not dropped:
                cleaned.append(cleaned_item)
        return cleaned, False

    return data, False


METER_VARIABLE_NAMES_TO_SKIP = {
    "zigbee:DemandDigitsLeft",
    "zigbee:DemandDigitsRight",
    "zigbee:DemandSuppressLeadingZero",
    "zigbee:SummationDigitsLeft",
    "zigbee:SummationDigitsRight",
    "zigbee:SummationSuppressLeadingZero",
}


def drop_unneeded_meter_variables(meter_data):
    """Remove formatting-only variables from meter component output."""
    for component in meter_data.get("components", []):
        variables = component.get("variables")
        if not isinstance(variables, list):
            continue

        component["variables"] = [
            var
            for var in variables
            if isinstance(var, Mapping)
            and var.get("name") not in METER_VARIABLE_NAMES_TO_SKIP
        ]
    return meter_data


def parse_scalar_value(value):
    """Parse scalar string values to bool/int/float when possible."""
    if isinstance(value, str):
        lower_value = value.lower()
        if lower_value == "true":
            return True
        if lower_value == "false":
            return False
        try:
            if any(ch in value for ch in (".", "e", "E")):
                return float(value)
            return int(value)
        except ValueError:
            return value
    return value


def sanitize_identifier(value):
    text = re.sub(r"[^A-Za-z0-9_]", "_", str(value or "unknown"))
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown"


def measurement_name_for_device(device):
    """Stable per-device measurement name."""
    hardware_address = device.get("hardware_address")
    if hardware_address:
        return f"device_{sanitize_identifier(hardware_address)}"
    return f"device_{sanitize_identifier(device.get('name'))}"


def meter_variables_to_fields(meter_data):
    """Flatten component variables to value-only field map."""
    fields = {}
    for component in meter_data.get("components", []):
        component_name = sanitize_identifier(component.get("name", "component"))
        variables = component.get("variables", [])
        if not isinstance(variables, list):
            continue

        for variable in variables:
            if not isinstance(variable, Mapping):
                continue
            variable_name = variable.get("name")
            if not variable_name:
                continue
            value = variable.get("value")
            if value is None:
                continue

            cleaned_name = str(variable_name).replace("zigbee:", "")
            key = f"{component_name}_{sanitize_identifier(cleaned_name)}"
            fields[key] = parse_scalar_value(value)
    return fields


def publish_devices_snapshot(devices):
    """Publish one flat point per device once per inventory refresh."""
    for device in devices:
        fields = {}
        for key, value in device.items():
            if value is None:
                continue
            fields[str(key)] = parse_scalar_value(value)
        influxdb_publish("devices", fields)


def influxdb_publish(measurement, data, tags=None):
    from influxdb import InfluxDBClient

    if not data:
        print("Not publishing empty data for:", measurement)
        return

    payload = {
        "measurement": measurement,
        "time": int(time.time()),
        "fields": flatten_for_influx(data),
    }

    if tags:
        payload["tags"] = {k: str(v) for k, v in tags.items() if v is not None}

    if not payload["fields"]:
        print("No scalar fields to publish for:", measurement)
        return

    try:
        client = InfluxDBClient(
            host=args.influxdb_host,
            port=args.influxdb_port,
            username=args.influxdb_user,
            password=args.influxdb_pass,
            database=args.influxdb_db,
        )
        if args.verbose:
            print(
                json.dumps(
                    {
                        "influxdb_write": {
                            "host": args.influxdb_host,
                            "port": args.influxdb_port,
                            "payload": payload,
                        }
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
            )
        client.write_points([payload], time_precision="s")
    except Exception as e:
        print("Failed to connect to InfluxDB: %s" % e)
        print("  Payload was: %s" % payload)


def collect_inventory_data(client):
    snapshot = {"timestamp": int(time.time())}

    wifi = client.wifi_status()
    snapshot["wifi_status"] = object_to_data(wifi)

    devices = client.device_list()
    device_rows = []
    for device in devices:
        hardware_address = getattr(device, "hardware_address", None)
        if not hardware_address:
            continue

        queried = client.device_query(hardware_address)
        queried_data = object_to_data(queried)
        queried_data = drop_null_value_objects(queried_data)[0]
        queried_data.pop("all_variables", None)
        queried_data.pop("components", None)
        queried_data.pop("device_details", None)

        device_rows.append(queried_data)

    snapshot["devices"] = device_rows
    return snapshot


def collect_meter_data(client, meter_addresses):
    snapshot = {"timestamp": int(time.time()), "meters": []}
    for hardware_address in meter_addresses:
        try:
            queried = client.device_query(hardware_address)
        except Exception:
            continue

        meter_data = object_to_data(queried)
        meter_data = drop_null_value_objects(meter_data)[0]
        meter_data.pop("all_variables", None)
        meter_data = drop_unneeded_meter_variables(meter_data)
        snapshot["meters"].append(meter_data)

    return snapshot


def meter_addresses_from_inventory(inventory):
    addresses = []
    for device in inventory.get("devices", []):
        if device.get("model_id") == "electric_meter" and device.get("hardware_address"):
            addresses.append(device["hardware_address"])
    return addresses


def main():
    if args.debug:
        eagle.localapi.enable_debug_logging()

    configure_eagle_timeout(args.eagle_timeout)
    client = eagle.LocalApi(
        host=args.eagle_host, username=args.eagle_user, password=args.eagle_pass
    )

    last_inventory_ts = 0
    meter_addresses = []
    last_published_contact_by_device = {}

    while True:
        now = time.time()
        should_refresh_inventory = (
            not meter_addresses
            or now - last_inventory_ts >= args.inventory_poll_interval
        )

        if should_refresh_inventory:
            inventory = collect_inventory_data(client)
            meter_addresses = meter_addresses_from_inventory(inventory)
            last_inventory_ts = now

            if args.raw:
                print(json.dumps({"inventory": inventory}, indent=2, sort_keys=True, default=str))

            if args.influxdb:
                publish_devices_snapshot(inventory.get("devices", []))

        meters = collect_meter_data(client, meter_addresses)
        if args.raw:
            print(json.dumps({"meters": meters}, indent=2, sort_keys=True, default=str))

        if args.influxdb:
            wifi_status = object_to_data(client.wifi_status())
            influxdb_publish("wifi_status", wifi_status)

            for meter in meters.get("meters", []):
                device_key = meter.get("hardware_address") or measurement_name_for_device(meter)
                last_contact = meter.get("last_contact")
                if (
                    last_contact is not None
                    and last_published_contact_by_device.get(device_key) == last_contact
                ):
                    if args.verbose:
                        print(
                            json.dumps(
                                {
                                    "influxdb_skip": {
                                        "measurement": measurement_name_for_device(meter),
                                        "reason": "last_contact_unchanged",
                                        "last_contact": last_contact,
                                    }
                                },
                                indent=2,
                                sort_keys=True,
                                default=str,
                            )
                        )
                    continue

                fields = meter_variables_to_fields(meter)
                influxdb_publish(measurement_name_for_device(meter), fields)
                if last_contact is not None:
                    last_published_contact_by_device[device_key] = last_contact

        time.sleep(args.meter_poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="",
    )

    parser.add_argument(
        "-r", "--raw", dest="raw", action="store_true", help="print json data to stdout"
    )

    parser.add_argument(
        "--eagle_host",
        dest="eagle_host",
        action="store",
        required=True,
        help="Eagle Local API host/ip",
    )
    parser.add_argument(
        "--eagle_user",
        dest="eagle_user",
        action="store",
        required=True,
        help="Eagle Cloud ID",
    )
    parser.add_argument(
        "--eagle_pass",
        dest="eagle_pass",
        action="store",
        required=True,
        help="Eagle Installation ID",
    )
    parser.add_argument(
        "--eagle_timeout",
        dest="eagle_timeout",
        action="store",
        default=30,
        type=int,
        help="Eagle API request timeout in seconds",
    )

    parser.add_argument(
        "--influxdb",
        dest="influxdb",
        action="store_true",
        help="publish to influxdb",
    )
    parser.add_argument(
        "--influxdb_host",
        dest="influxdb_host",
        action="store",
        default="localhost",
        help="hostname of InfluxDB HTTP API",
    )
    parser.add_argument(
        "--influxdb_port",
        dest="influxdb_port",
        action="store",
        default=8086,
        type=int,
        help="port of InfluxDB HTTP API",
    )
    parser.add_argument(
        "--influxdb_user",
        dest="influxdb_user",
        action="store",
        help="InfluxDB username",
    )
    parser.add_argument(
        "--influxdb_pass",
        dest="influxdb_pass",
        action="store",
        help="InfluxDB password",
    )
    parser.add_argument(
        "--influxdb_db",
        dest="influxdb_db",
        action="store",
        default="eagle",
        help="InfluxDB database name",
    )

    parser.add_argument(
        "-v", "--verbose", dest="verbose", action="store_true", help="verbose mode"
    )
    parser.add_argument(
        "--debug",
        dest="debug",
        action="store_true",
        help="enable verbose Eagle API debug logging",
    )
    parser.add_argument(
        "--meter_poll_interval",
        dest="meter_poll_interval",
        action="store",
        default=30,
        type=int,
        help="meter polling interval in seconds",
    )
    parser.add_argument(
        "--inventory_poll_interval",
        dest="inventory_poll_interval",
        action="store",
        default=86400,
        type=int,
        help="device inventory polling interval in seconds",
    )

    args = parser.parse_args()
    main()
