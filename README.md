# Kasa Christmas Lights

A local dashboard for the six independently switched AC outlets on a TP-Link Kasa HS300.

## Set up

From the repository directory, with Python 3.11 or newer installed:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
```

1. Connect the HS300 to your Wi-Fi with the Kasa app. Run `kasa discover` from the virtual environment or check your router's device list to find its IP address. A DHCP reservation keeps the address stable.
2. Edit `config.json` with the strip's IP, your latitude and longitude, and your IANA time zone, such as `America/Chicago`. The example coordinates are for Chicago.
3. Set each outlet's light name and `on_start` setting. Outlet numbers run from 1 through 6 in the same order as the strip reports them. Verify the mapping against your strip with a single light when setting up. Omit unused outlets from `lights`. The server keeps those outlets off.
4. Run `python server.py` from the virtual environment. If your device requires TP-Link authentication, supply `KASA_USERNAME` and `KASA_PASSWORD` as environment variables before starting it.

Open `http://localhost:8080` on the server. On other devices on the same Wi-Fi, open `http://<server-LAN-IP>:8080` using the server's IPv4 address, separate from the strip's address. On a Mac, look under System Settings → Wi-Fi → Details → TCP/IP. Allow incoming connections if the host firewall prompts. Anyone who can reach the dashboard can use it, so keep it on your trusted local network and leave router port forwarding disabled.

Use `--config /path/to/config.json` to select another config file or `--port 8081` to change the listening port. Restart the server after editing the config.

## Schedule

At sunset, outlets with `on_start: true` turn on. All others start off. Until sunrise, dashboard users can switch each mapped outlet independently. At sunrise, the server turns all six outlets off and disables manual controls. The server calculates solar times locally and checks the strip roughly every five seconds. It reconciles any changes made directly on the strip or in the Kasa app.

The server saves dashboard overrides next to the config in a local `*.overrides.json` file. They survive a restart during the same night and expire at sunrise. If the strip drops off Wi-Fi, the server applies the current schedule when it reconnects. Keep the host awake and the server running for transitions to happen on time.
