
# netatmo - WeeWX Driver for Netatmo Weather Stations

[![License](https://img.shields.io/badge/license-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.en.html)

## Introduction
This is the netatmo driver for WeeWX, a free, open-source weather station software. The driver allows you to retrieve data from your Netatmo weather station and integrate it seamlessly into WeeWX.

## Features
- Two Modes of Operation: This driver can use the netatmo API to obtain data from the netatmo servers (`cloud`) or parse the packets sent from a netatmo station (`sniff`). The latter works only with netatmo firmware 101 (circa early 2015), see why in the Firmware 102 note. By default, this driver will operate in 'cloud' mode.
- Compatibility: The driver is compatible with both Python 2.7 and 3.x and supports WeeWX 4.* versions as well as earlier versions.
- Automatic Token Handling: Communication with the netatmo servers requires `refresh_token`, `client_id`, and `client_secret`. The `refresh_token` is the one you can find on your application page after creating a new token. The `client_id` and `client_secret` must be obtained via the dev.netatmo.com web site. Using these 3 things, the driver automatically obtains and updates the tokens needed to get data from the server.
- Enhanced Rain Data Handling: Special logic is included to address discrepancies in rain data retrieval from the netatmo API, ensuring accurate rain summaries in WeeWX.

## ⚠️Note on Firmware 102
Firmware 102 introduced arbitrary encryption in response to a poorly chosen decision to include the wifi password in network traffic sent to the netatmo servers. Unfortunately, the encryption blocks end-users from accessing their data, while the netatmo stations might still send information such as the wifi password to the netatmo servers.

## Installation Instructions

1. Download the driver:
    ```
    wget -O weewx-netatmo.zip https://github.com/Buco7854/weewx-netatmo/archive/master.zip
    ```
2. Install the driver:
    ```
    sudo wee_extension --install weewx-netatmo.zip
    ```
   or for WeeWX 5.0
   ```
   weectl extension install weewx-netatmo.zip
   ```
3. Configure the driver:
   Edit the `/etc/weewx/weewx.conf`, see [Configuration](#configuration).

4. Restart WeeWX:
    ```
    sudo /etc/init.d/weewx restart
    ```
   or
   ```
   sudo systemctl restart weewx.service
   ```
   

## Configuration
For communication with the netatmo servers, you will need to add these parameters to your WeeWX configuration :
- `tokens_persistence_file`: The path to a json file that requires a `refresh_token` key. Obtain this from your application page after creating a new token. You must **NEVER** delete the file since it will contain a temporary token that will be reused between restart. 

   For example your `tokens_persistence_file` will be `/etc/weewx/tokens_persistence_file.json` and will look as followed:
   ```json
  {"refresh_token":"YOUR_TOKEN"}
   ```
  ⚠️ Make sure weewx user can read and write in this file.


- `client_id` and `client_secret`: These must be obtained via the dev.netatmo.com website.

## License
This driver is distributed under the GPLv3 license. See [LICENSE](LICENSE) for more information.

## Support
For questions, bug reports, or feature requests, please create an issue on the [GitHub repository](https://github.com/Buco7854/weewx-netatmo).

## Contributors
- matthewwall (Original Author) - [Original Repository](https://github.com/matthewwall/weewx-netatmo)
- bricebou (Contributor) - [Original Fork](https://github.com/bricebou/weewx-netatmo)
- jkrasinger (Contributor) - [Merged Fork](https://github.com/jkrasinger/weewx-netatmo)
