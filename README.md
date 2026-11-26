# Kasa Christmas Lights

A local dashboard for the six independently switched AC outlets on a TP-Link Kasa HS300.

## Set up

For a fresh checkout, open Terminal in the repository directory with Python 3.11 or newer installed. Skip the virtual environment setup if `.venv` is already installed. The last command preserves an existing `config.json`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n config.example.json config.json
```

1. Connect the HS300 to your Wi-Fi with the Kasa app. Run `.venv/bin/kasa discover` or check your router's device list to find its IP address. A DHCP reservation keeps the address stable.
2. Edit `config.json` with the strip's IP and the location where it runs. Replace `null` in `latitude` and `longitude` with decimal numbers, without quotes, and set `timezone` to your IANA time zone. For example, Chicago uses `"latitude": 41.8781`, `"longitude": -87.6298`, and `"timezone": "America/Chicago"`. The server needs these settings to calculate sunrise and sunset.
3. Set each outlet's light name and `on_start` setting. Outlet numbers run from 1 through 6 in the same order as the strip reports them. Verify the mapping against your strip with a single light when setting up. Omit unused outlets from `lights`. The server keeps those outlets off.

## Start the server on macOS

Save your TP-Link credentials in one password item named `kasa-light-srv` in the login Keychain. Set the item's account name to your TP-Link email. From the repository directory, run `./start.sh`. The launcher reads the email and password from that item. If macOS asks whether `security` can access the item, choose **Allow** for this launch.

### Start without Keychain

Open Terminal and change to the repository directory. For a checkout at `~/git/kasa-light-srv`, run the following in the default macOS shell (zsh).

```zsh
cd ~/git/kasa-light-srv
read -r 'KASA_USERNAME?TP-Link email: '
read -rs 'KASA_PASSWORD?TP-Link password: '
echo
KASA_USERNAME="$KASA_USERNAME" KASA_PASSWORD="$KASA_PASSWORD" .venv/bin/python server.py
unset KASA_USERNAME KASA_PASSWORD
```

Enter the email and password for the TP-Link account linked to the strip. The password prompt hides what you type. The command passes the credentials to the server process, so `config.json` only needs the strip address, location, and outlet settings. If your strip works without authentication, run `.venv/bin/python server.py` directly from the repository directory.

Wait for `Running on http://0.0.0.0:8080` in Terminal. Leave that window open while the server runs. Press `Ctrl+C` to stop it.

Open `http://localhost:8080` on the server. On other devices on the same Wi-Fi, open `http://<server-LAN-IP>:8080` using the server's IPv4 address, separate from the strip's address. On a Mac, look under System Settings → Wi-Fi → Details → TCP/IP. Allow incoming connections if the host firewall prompts. Anyone who can reach the dashboard can use it, so keep it on your trusted local network and leave router port forwarding disabled.

Use `--config /path/to/config.json` to select another config file or `--port 8081` to change the listening port. Restart the server after editing the config.

## Watch mode for development

Run `./start.sh --watch` from the repository directory. The server restarts when `server.py`, `index.html`, or the selected config file changes. The launcher reads Keychain once for the session. Press `Ctrl+C` to stop both the watcher and server.

## Schedule

At sunset, outlets with `on_start: true` turn on. All others start off. Until sunrise, dashboard users can switch each mapped outlet independently. At sunrise, the server turns all six outlets off and disables manual controls. The server calculates solar times locally and checks the strip roughly every five seconds. It reconciles any changes made directly on the strip or in the Kasa app.

The server saves dashboard overrides next to the config in a local `*.overrides.json` file. They survive a restart during the same night and expire at sunrise. If the strip drops off Wi-Fi, the server applies the current schedule when it reconnects. Keep the host awake and the server running for transitions to happen on time.

## Window decorations and multiple strips

The example single-strip config still works. To map decorations across strips, replace `strip_host` and `lights` with a layout like the one below and keep your existing `latitude`, `longitude`, and `timezone` at the top level. If your strips use TP-Link authentication, they must accept the same account credentials.

```json
{
  "strips": {
    "front": {"alias": "Front Windows"},
    "upstairs": {"alias": "Upstairs Windows"}
  },
  "lights": {
    "peppermints": {
      "name": "Peppermints", "window": "Front bay",
      "strip": "front", "port": 2, "on_start": true
    },
    "star": {
      "name": "Star", "window": "Upstairs landing",
      "strip": "upstairs", "port": 1, "on_start": false
    }
  }
}
```

Copy each strip's exact friendly name from the Kasa app into `alias`. Give each HS300 a unique name. The server discovers alias-based strips on the local network at startup and looks them up again after a disconnect, so their IP addresses can change. You can mix aliases with IP strings, such as `"upstairs": "192.0.2.12"`. Restart the server after editing the config.

Use letters, numbers, hyphens, or underscores for each light ID. Keep that ID when moving a decoration to a different outlet. Each strip and outlet pair can be assigned to one light. The optional window label appears on the dashboard. The server keeps unused outlets on every configured strip off.

## Random rotation

Rotation is on by default and picks one random decoration across all strips every five minutes. Use **Enable rotation** on the dashboard to change the shared setting. Your choice survives a restart. The first pick is due five minutes after sunset, or five minutes after enabling rotation during the night. Each pick turns off the other outlets and avoids repeating the previous pick when at least two decorations are configured.

Each manual switch change resets the shared five-minute countdown. When it expires, rotation picks a decoration and replaces the manual settings. Turning rotation off returns to the sunset defaults if a random pick was active, or keeps any current manual settings. Rotation stops at sunrise and the enabled setting carries forward to the next night. The dashboard shows the current phase and time remaining.
