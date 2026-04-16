#!/usr/bin/python3

import argparse
import json
import re
import time
from collections.abc import Mapping, Sequence
from xml.etree import ElementTree

import requests


def safe_local_call(description, callback, default=None):
    """Run direct local API call and return default on failures."""
    try:
        return callback()
    except Exception as e:
        print(f"Eagle local API call failed ({description}): {e}")
        return default


def to_snake_case(name):
    text = str(name or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return text.strip("_").lower()


def text_or_none(element):
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value if value else None


def parse_variable(variable_element):
    # device_query responses use nested fields; device_details may return text-only variables.
    children = list(variable_element)
    if not children:
        return {"name": text_or_none(variable_element)}

    parsed = {}
    for child in children:
        parsed[to_snake_case(child.tag)] = text_or_none(child)
    return parsed


def parse_component(component_element):
    parsed = {}
    for child in component_element:
        key = to_snake_case(child.tag)
        if key == "variables":
            parsed["variables"] = [
                parse_variable(variable_element)
                for variable_element in child.findall("./Variable")
            ]
        else:
            parsed[key] = text_or_none(child)
    return parsed


def parse_device_response(xml_text):
    root = ElementTree.fromstring(xml_text)
    if root.tag != "Device":
        raise ValueError(f"Expected <Device> response, got <{root.tag}>")

    device = {}
    details = root.find("./DeviceDetails")
    if details is not None:
        details_data = {}
        for child in details:
            details_data[to_snake_case(child.tag)] = text_or_none(child)
        device.update(details_data)

    components = root.findall("./Components/Component")
    if components:
        device["components"] = [parse_component(component) for component in components]

    return device


def parse_device_list_response(xml_text):
    root = ElementTree.fromstring(xml_text)
    if root.tag != "DeviceList":
        raise ValueError(f"Expected <DeviceList> response, got <{root.tag}>")

    devices = []
    for device_element in root.findall("./Device"):
        row = {}
        for child in device_element:
            row[to_snake_case(child.tag)] = text_or_none(child)
        devices.append(row)

    return devices


def post_local_command(command_xml):
    url = f"http://{args.eagle_host}/cgi-bin/post_manager"
    if args.debug:
        print("--- Eagle Local API Request ---")
        print(command_xml)
        print()
    response = requests.post(
        url=url,
        data=command_xml,
        headers={"Content-Type": "text/xml"},
        auth=(args.eagle_user, args.eagle_pass),
        timeout=args.eagle_timeout,
    )
    response.raise_for_status()
    if args.debug:
        print("--- Eagle Local API Response ---")
        print(response.text)
        print()
    return response.text


def fetch_device_list():
    command_xml = "\r\n".join(
        [
            "<Command>",
            "  <Name>device_list</Name>",
            "</Command>",
        ]
    )
    return parse_device_list_response(post_local_command(command_xml))


def fetch_device_query_all(hardware_address):
    command_xml = "\r\n".join(
        [
            "<Command>",
            "  <Name>device_query</Name>",
            "  <DeviceDetails>",
            f"    <HardwareAddress>{hardware_address}</HardwareAddress>",
            "  </DeviceDetails>",
            "  <Components>",
            "    <All>Y</All>",
            "  </Components>",
            "</Command>",
        ]
    )
    return parse_device_response(post_local_command(command_xml))


def fetch_wifi_status():
    command_xml = "\r\n".join(
        [
            "<Command>",
            "  <Name>wifi_status</Name>",
            "</Command>",
        ]
    )
    response_text = post_local_command(command_xml).strip()
    if not response_text:
        return {}
    try:
        root = ElementTree.fromstring(response_text)
    except ElementTree.ParseError:
        return {"raw": response_text}

    wifi_data = {}
    for child in root:
        wifi_data[to_snake_case(child.tag)] = text_or_none(child)
    return wifi_data


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


def normalize_wifi_status_fields(wifi_status):
    """Normalize wifi_status types to match stable Influx field schema."""
    if not isinstance(wifi_status, Mapping):
        return wifi_status

    normalized = {}
    for key, value in wifi_status.items():
        key_str = str(key)
        if value is None:
            continue

        if key_str == "last_up_time":
            normalized[key_str] = parse_scalar_value(value)
        else:
            normalized[key_str] = str(value)

    return normalized


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


def object_name_for_hardware(prefix, hardware_address):
    if hardware_address:
        return f"{prefix}_{hardware_address}"
    return f"{prefix}_unknown"


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


def collect_inventory_data():
    snapshot = {"timestamp": int(time.time()), "ok": True}

    wifi_status = safe_local_call("wifi_status", fetch_wifi_status)
    if wifi_status is not None:
        snapshot["wifi_status"] = normalize_wifi_status_fields(wifi_status)

    devices = safe_local_call("device_list", fetch_device_list, default=[])
    if not devices:
        snapshot["ok"] = False
    snapshot["devices"] = devices
    return snapshot


def collect_meter_data(meter_addresses, device_shapes_by_address=None):
    snapshot = {
        "timestamp": int(time.time()),
        "meters": [],
        "devices": [],
    }
    for hardware_address in meter_addresses:
        queried = safe_local_call(
            f"device_query:{hardware_address}",
            lambda addr=hardware_address: fetch_device_query_all(addr),
        )
        if queried is None:
            continue

        meter_data = drop_null_value_objects(queried)[0]
        meter_data.pop("all_variables", None)
        meter_data.pop("device_details", None)
        device_shape = (device_shapes_by_address or {}).get(hardware_address, {})
        if device_shape:
            device_data = {
                key: meter_data.get(key)
                for key in device_shape.keys()
                if meter_data.get(key) is not None
            }
            # Preserve network_interface from device_query for compatibility.
            network_interface = meter_data.get("network_interface")
            if network_interface is not None:
                device_data["network_interface"] = network_interface
        else:
            # Fallback if inventory shape is unavailable.
            device_data = {
                key: value
                for key, value in meter_data.items()
                if key not in {"all_variables", "components", "device_details"}
                and value is not None
            }
        meter_data = drop_unneeded_meter_variables(meter_data)
        meter_payload = {"components": meter_data.get("components", [])}

        snapshot["devices"].append(device_data)
        snapshot["meters"].append(meter_payload)

    return snapshot


def meter_addresses_from_inventory(inventory):
    addresses = []
    for device in inventory.get("devices", []):
        if device.get("model_id") == "electric_meter" and device.get("hardware_address"):
            addresses.append(device["hardware_address"])
    return addresses


def main():
    if args.debug:
        print("Debug mode: direct Local API XML calls enabled")

    last_inventory_ts = 0
    meter_addresses = []
    device_shapes_by_address = {}
    last_published_contact_by_device = {}

    while True:
        now = time.time()
        should_refresh_inventory = (
            not meter_addresses
            or now - last_inventory_ts >= args.inventory_poll_interval
        )

        if should_refresh_inventory:
            inventory = collect_inventory_data()
            if inventory.get("ok"):
                meter_addresses = meter_addresses_from_inventory(inventory)
                device_shapes_by_address = {
                    device.get("hardware_address"): device
                    for device in inventory.get("devices", [])
                    if device.get("hardware_address")
                }
                last_inventory_ts = now
            else:
                print("Inventory refresh incomplete; reusing previous meter list")

            if args.raw:
                print(json.dumps({"inventory": inventory}, indent=2, sort_keys=True, default=str))

            if args.influxdb:
                publish_devices_snapshot(inventory.get("devices", []))
                if inventory.get("wifi_status"):
                    influxdb_publish("wifi_status", inventory.get("wifi_status"))

        meters = collect_meter_data(meter_addresses, device_shapes_by_address)
        if args.raw:
            print(json.dumps({"meters": meters}, indent=2, sort_keys=True, default=str))

        if args.influxdb:
            for device_data, meter_data in zip(
                meters.get("devices", []), meters.get("meters", [])
            ):
                hardware_address = device_data.get("hardware_address")
                meter_object_name = object_name_for_hardware("device", hardware_address)

                device_key = hardware_address or "unknown"
                last_contact = device_data.get("last_contact")
                if (
                    last_contact is not None
                    and last_published_contact_by_device.get(device_key) == last_contact
                ):
                    if args.verbose:
                        print(
                            json.dumps(
                                {
                                    "influxdb_skip": {
                                        "measurement": meter_object_name,
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

                publish_devices_snapshot([device_data])
                meter_fields = meter_variables_to_fields(meter_data)
                influxdb_publish(meter_object_name, meter_fields)
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
        help="Eagle Local API request timeout in seconds",
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
        help="print direct Local API mode debug notice",
    )
    parser.add_argument(
        "--meter_poll_interval",
        dest="meter_poll_interval",
        action="store",
        default=10,
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
