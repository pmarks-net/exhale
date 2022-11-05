# exhale
CO2 monitor with Z-wave fan control for Raspberry Pi

My goal is to connect a CO2 sensor and Z-wave controller to a Raspberry Pi, and run the bathroom exhaust fan whenever CO2 levels are high. The device should operate with a read-only filesystem and no internet access.

## Hardware I'm using
- Raspberry Pi 3 B+
- CanaKit Premium Black Case hot glued to a MicroUSB power supply
- HUSBZ-1 USB Z-wave controller or Z-Wave.Me RaZberry2
- Adafruit SCD-30 CO2 sensor
- UltraPro Z-Wave Plus toggle switch

## How to install
- TODO: Pin diagram
- Burn Raspberry Pi OS Lite to an SD card
- Configure WiFi and/or SSH if desired
- Install random stuff:
  ```shell
  sudo apt install -y git screen python3-pip
  pip3 install adafruit-circuitpython-scd30 adafruit-extended-bus
  ```
- Apparently python-openzwave 0.4.19 doesn't install on RPiOS 11, so perhaps it was a bad idea to depend on this library, but for now it's still buildable:

  ```shell
  sudo apt install -y cython3 libopenzwave1.6-dev
  git clone https://github.com/OpenZWave/python-openzwave.git
  cd python-openzwave
  sed -i 's/Cython==0.28.6/Cython>=0.29/' pyozw_setup.py
  ./setup-lib.py install --user --flavor=shared
  ```

- Add stuff to `/boot/config.txt`:
  ```
  # /dev/ttyS0 (zwave controller) on GPIO 2-3:
  enable_uart=1
  # /dev/i2c-6 (scd30) on GPIO 9-10:
  dtparam=i2c_arm=on
  dtoverlay=i2c-gpio,bus=6,i2c_gpio_scl=9,i2c_gpio_sda=10
  ```

- Make `./exhale.py reset` work, followed by `./exhale.py co2`:
  ```shell
  $ git clone https://github.com/pmarks-net/exhale.git
  $ cd exhale
  $ ./exhale.py --help
  === subcommand 'reset' ===
  usage: exhale.py reset [-h] --zdevice /dev/ttyX --switches N

  Reinitialize the ZWave network. Before running this command, all switches must
  be in the 'factory reset' state. To factory reset an UltraPro Z-Wave toggle
  switch, quickly press 'up up up down down down'. Later when prompted, press
  'up' to add each switch to the ZWave network.

  optional arguments:
    -h, --help           show this help message and exit
    --zdevice /dev/ttyX  ZWave serial device
    --switches N         Number of switches to add

  === subcommand 'co2' ===
  usage: exhale.py co2 [-h] --zdevice /dev/ttyX [--scd30_i2c 6]
                       [--co2_limit 800] [--co2_diff 50] [--manual 3600]

  Run the daemon to monitor CO₂ levels and control exhaust fans.

  optional arguments:
    -h, --help           show this help message and exit
    --zdevice /dev/ttyX  ZWave serial device
    --scd30_i2c 6        Read from SCD30 at /dev/i2c-N; requires (e.g.)
                         dtoverlay=i2c-gpio,bus=6,i2c_gpio_scl=9,i2c_gpio_sda=10
    --co2_limit 800      Enable fan when CO₂ level exceeds this ppm value
    --co2_diff 50        Disable fan when CO₂ level falls below (limit-diff)
    --manual 3600        When a switch is toggled manually, disable automatic
                         control for this many seconds
  ```

- Add stuff to `/etc/rc.local`:
  ```shell
  # The LED blinker won't work without this:
  chmod a+w /sys/class/leds/led1/brightness
  # Use `screen -r` to see logs and debug:
  su pi -c "/home/pi/exhale/daemon.sh"
  ```

- Enable overlay file system, for read-only SD card: https://learn.adafruit.com/read-only-raspberry-pi
