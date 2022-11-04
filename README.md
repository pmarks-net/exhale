# exhale
CO2 monitor with Z-wave fan control for Raspberry Pi

This is a WORK IN PROGRESS, and not ready for use.

My goal is to connect a CO2 sensor and Z-wave controller to a Raspberry Pi, and run the bathroom exhaust fan whenever CO2 levels are high. The device should operate with a read-only filesystem and no internet access.

### Hardware I'm using
- Raspberry Pi 3 B+
- CanaKit Premium Black Case hot glued to a MicroUSB power supply
- HUSBZ-1 USB Z-wave controller or Z-Wave.Me RaZberry2
- Adafruit SCD-30 CO2 sensor
- UltraPro Z-Wave Plus toggle switch

### Installing python-openzwave

`libopenzwave` looks unmaintained, so it was probably a bad idea to depend on it.
That said, I was able to get it running on Python 3.9:

    sudo apt install cython3 libopenzwave1.6-dev
    git clone https://github.com/OpenZWave/python-openzwave.git
    cd python-openzwave
    sed -i 's/Cython==0.28.6/Cython>=0.29/' pyozw_setup.py
    ./setup-lib.py install --user --flavor=shared
