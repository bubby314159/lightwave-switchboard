# Lightwave Switchboard

A lightweight self-hosted web control panel for first-generation LightwaveRF Link and Connect Bridge devices.

Switchboard provides a modern browser-based interface for controlling sockets, dimmers, and rooms from any device on your local network. It bridges the gap between modern web browsers and the legacy LightwaveRF Gen 1 UDP protocol.

## Features

* Web-based control panel accessible from phones, tablets, and desktops
* Support for LightwaveRF Gen 1 Link / Connect Bridge devices
* On/off switching and dimmer controls
* Room-based organization
* Device pairing directly from the web interface
* Automatic configuration persistence
* Responsive mobile-friendly UI
* Dark and light mode support
* No external dependencies
* Works with Python 3.8+

## Requirements

* Python 3.8 or newer
* A LightwaveRF Gen 1 Link or Connect Bridge
* Local network access to the bridge

No additional packages are required.

## Quick Start

Clone the repository and run:

```bash
python3 switchboard.py
```

Then open:

```text
http://localhost:8080
```

Or from another device on your network:

```text
http://<server-ip>:8080
```

## Configuration

Switchboard automatically stores configuration in:

```text
switchboard.json
```

The configuration contains:

* Bridge IP address
* Room definitions
* Device definitions
* Last known device states
* Communication preferences

## Pairing Devices

1. Open **Settings**.
2. Click **Pair now**.
3. Press the button on your LightwaveRF Link.
4. Wait for confirmation.

Once paired, Switchboard can send commands to all configured devices.

## Command Line Options

```bash
python3 switchboard.py [options]
```

### Available Options

| Option         | Description                          |
| -------------- | ------------------------------------ |
| `--port`       | Web server port (default: 8080)      |
| `--host`       | Address to listen on                 |
| `--link-ip`    | Manually specify the Link IP address |
| `--config`     | Configuration file location          |
| `--allow-host` | Additional allowed hostnames         |
| `--verbose`    | Enable request logging               |

### Example

```bash
python3 switchboard.py \
  --port 8080 \
  --link-ip 192.168.1.50 \
  --verbose
```

## Security

Switchboard is designed for use on trusted local networks.

Recommendations:

* Do not expose the web interface directly to the internet.
* Restrict access using your firewall when possible.
* Run behind a VPN if remote access is required.
* Keep the host machine updated.

## How It Works

Modern browsers cannot communicate directly with the LightwaveRF Gen 1 bridge because the bridge only accepts UDP commands.

Switchboard acts as an intermediary:

```text
Browser
    │
HTTP/JSON
    │
Switchboard
    │
UDP
    │
LightwaveRF Link
```

The web server receives commands from the browser and relays them to the bridge using the legacy UDP protocol.

## Project Goals

* Preserve support for legacy LightwaveRF Gen 1 hardware
* Provide a clean and modern user experience
* Remain dependency-free and easy to deploy
* Run reliably on low-power devices such as Raspberry Pi systems

## License

MIT License

See `LICENSE` for details.
