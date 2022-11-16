#!/usr/bin/env python3

import argparse
import asyncio
import collections
import datetime
import glob
import math
import os
import re
import tempfile
import time
import traceback
from enum import Enum

import libopenzwave

# pip3 install adafruit-circuitpython-scd30
import adafruit_scd30

# pip3 install adafruit-extended-bus
import adafruit_extended_bus


class SwitchState(Enum):
    ALIVE = 1
    ON = 2
    OFF = 3
    WANT_ON = 4
    WANT_OFF = 5


class SwitchAlive(Exception):
    pass


class SwitchToggled(Exception):
    pass


class FanState(Enum):
    ON = True
    OFF = False


class Switch:
    def __init__(self, node_id, switch_id, manager_set_value, manual_secs):
        self.node_id = node_id
        self.switch_id = switch_id
        self.manager_set_value = manager_set_value
        self.manual_secs = manual_secs
        self.onoff = False
        self.want_onoff = None
        self.task = asyncio.create_task(self.run())

        # Queue of SwitchState enums.
        self.q = asyncio.Queue()

    def __str__(self):
        return "Switch node_id=%r, switch_id=%r, onoff=%r" % (self.node_id, self.switch_id, self.onoff)

    def set_alive(self):
        self.q.put_nowait(SwitchState.ALIVE)

    def set_onoff(self, v):
        if v:
            self.q.put_nowait(SwitchState.ON)
        else:
            self.q.put_nowait(SwitchState.OFF)

    def set_want_onoff(self, v):
        if v:
            self.q.put_nowait(SwitchState.WANT_ON)
        else:
            self.q.put_nowait(SwitchState.WANT_OFF)

    async def run(self):
        try:
            await self.run_or_die()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            raise

    async def run_or_die(self):
        # Wait for first ALIVE.
        try:
            while True:
                await self.eat_q(duration=None)
        except SwitchAlive:
            pass
        while True:
            try:
                await self.alive_loop()
            except SwitchAlive:
                print("Rebooting alive_loop")

    async def alive_loop(self):
        # How long to ignore manual toggles after a state change.
        DEBOUNCE = 5.0  # seconds

        try:
            # Send (off 1sec, on 1sec, off) to tell humans that the switch is
            # under automatic control.
            print("Sending ALIVE pulse")
            await self.send_and_ignore(False, 1.0)
            await self.send_and_ignore(True, 1.0)
            await self.send_and_debounce(False, DEBOUNCE)

            while True:
                # Control the switch automatically.
                if self.want_onoff not in (None, self.onoff):
                    await self.send_and_debounce(self.want_onoff, DEBOUNCE)

                # Wait for humans to mess with the switch.
                await self.eat_q(duration=None, monitor_toggled=True)

        except SwitchToggled:
            print(f"Begin manual override ({self.manual_secs}s)")
            while True:
                try:
                    await self.eat_q(duration=self.manual_secs, monitor_toggled=True)
                except SwitchToggled:
                    print(f"Restart manual override ({self.manual_secs}s)")
                    continue
                else:
                    print("Back to automatic control")
                    break

    async def send_and_ignore(self, value, duration):
        print("send_and_ignore", value, duration)
        self.manager_set_value(self.switch_id, value)
        await self.eat_q(duration=duration)

    async def send_and_debounce(self, value, duration):
        print("send_and_debounce", value, duration)
        self.manager_set_value(self.switch_id, value)
        # Wait for the state to settle.
        await self.eat_q(duration=duration)
        # If it settled to the wrong value, blame the human.
        if self.onoff != value:
            raise SwitchToggled

    async def eat_q(self, duration, monitor_toggled=False):
        if duration is None:
            # Wait indefinitely for the first event,
            # then stop as soon as the queue is empty.
            stop_on_empty = True
        else:
            # Wait until the duration expires.
            wait_until = time.monotonic() + duration
            stop_on_empty = False

        alive = False
        toggled = False

        while True:
            try:
                v = await asyncio.wait_for(self.q.get(), duration)
                #print("eat_q v=", v)
            except asyncio.TimeoutError:
                #print("eat_q timeout", alive, toggled)
                if alive:
                    raise SwitchAlive
                if toggled:
                    raise SwitchToggled
                return

            if v == SwitchState.ALIVE:
                alive = True
                stop_on_empty = True
            elif v in (SwitchState.ON, SwitchState.OFF):
                onoff = (v == SwitchState.ON)
                #print("onoff=%r" % onoff)
                if self.onoff != onoff:
                    self.onoff = onoff
                    if monitor_toggled:
                        print("TOGGLED!")
                        toggled = True
                        stop_on_empty = True
            elif v in (SwitchState.WANT_ON, SwitchState.WANT_OFF):
                self.want_onoff = (v == SwitchState.WANT_ON)
                #print("want_onoff=%r" % self.want_onoff)

            if stop_on_empty:
                duration = 0
            else:
                duration = wait_until - time.monotonic()


def is_a_switch(zwargs):
    return (zwargs["valueId"]["commandClass"] == "COMMAND_CLASS_SWITCH_BINARY" and
            zwargs["valueId"]["index"] == 0)


class StateTracker:
    def __init__(self, manager_set_value, manual_secs):
        self._manager_set_value = manager_set_value
        self._manual_secs = manual_secs
        self._loop = asyncio.get_running_loop()
        self._q = asyncio.Queue()
        self._nodes_queried = False
        self.switches = {}
        self.home_id = None

    def threadsafe_watcher_cb(self, zwargs):
        #print(f"zwave event: {datetime.datetime.now().isoformat(sep=' ')} {zwargs}")
        self._loop.call_soon_threadsafe(lambda: self._q.put_nowait(zwargs))

    async def wait_for_nodes(self):
        if self.home_id is not None:
            raise AssertionError("Can't wait_for_nodes() with existing home_id")
        zwargs = await self._match("DriverReady")
        self.home_id = zwargs["homeId"]
        await self._match("AllNodesQueried|AllNodesQueriedSomeDead|AwakeNodesQueried")
        self._nodes_queried = True
        for switch in self.switches.values():
            switch.set_alive()

    async def wait_for_driver_removed(self):
        await self._match("DriverRemoved")
        self.home_id = None
        self._nodes_queried = False
        for switch in self.switches.values():
            switch.task.cancel()
        self.switches.clear()

    async def wait_for_controller_state(self, cs):
        return await self._match("ControllerCommand", lambda z: z["controllerState"] == cs)

    async def wait_for_switch_added(self):
        zwargs = await self._match(
                "ValueAdded",
                is_a_switch,
                timeout=15*60)  # Wait 15 minutes for user to add the switch.
        return zwargs["valueId"]["id"]

    async def wait_until(self, mono_ts):
        while True:
            timeout = mono_ts - time.monotonic()
            if timeout <= 0:
                break
            try:
                await self._q_get(timeout)
            except asyncio.TimeoutError:
                pass

    # notify_types = "Type1|Type2|..."
    # zwargs_matcher = f(zwargs) -> True if match.
    async def _match(self, notify_types, zwargs_matcher=None, timeout=60):
        notify_types = notify_types.split("|")
        note =  " with zwargs_matcher" if zwargs_matcher else ""
        print(f"=== Waiting for {notify_types}{note} ===")
        while True:
            start = time.monotonic()
            zwargs = await self._q_get(timeout=timeout)
            timeout -= (time.monotonic() - start)
            if zwargs["notificationType"] not in notify_types:
                continue
            if zwargs_matcher and not zwargs_matcher(zwargs):
                continue
            return zwargs

    async def _q_get(self, timeout):
        zwargs = await asyncio.wait_for(self._q.get(), timeout=timeout)
        self._q.task_done()

        # Check for events that we're always waiting for.
        ntype = zwargs["notificationType"]
        if ntype == "ValueAdded" and is_a_switch(zwargs):
            node_id = zwargs["nodeId"]
            switch_id = zwargs["valueId"]["id"]
            switch = Switch(node_id, switch_id, self._manager_set_value, self._manual_secs)
            try:
                self.switches[node_id].task.cancel()
                print("Destroyed duplicate switch with node_id %r" % node_id)
            except KeyError:
                pass
            self.switches[node_id] = switch
            print(f"Tracking {switch}")
        elif ntype == "ValueChanged" and is_a_switch(zwargs):
            node_id = zwargs["nodeId"]
            switch_id = zwargs["valueId"]["id"]
            onoff = zwargs["valueId"]["value"]
            try:
                switch = self.switches[node_id]
                if switch.switch_id != switch_id:
                    raise KeyError
            except KeyError:
                print("Unknown switch %r" % node_id)
            else:
                switch.set_onoff(onoff)
        elif ntype == "Notification" and zwargs["notificationCode"] == 6:
            node_id = zwargs["nodeId"]
            try:
                switch = self.switches[node_id]
            except KeyError:
                pass
            else:
                if self._nodes_queried:
                    switch.set_alive()
                print("Switch %r alive" % node_id)

        return zwargs


class Averager:
    def __init__(self, twindow):
        self.q = collections.deque()
        self.twindow = twindow  # Average over this time window (seconds)

    def add(self, now, value):
        if self.q and self.q[0][0] > now:
            raise AssertionError("must use time.monotonic()")

        # Add new value, and purge values older than twindow.
        self.q.appendleft((now, value))
        while self.q[-1][0] <= now - self.twindow:
            self.q.pop()

    def is_fresh(self, now):
        # Is the latest value still within the window?
        return self.q and self.q[0][0] > now - self.twindow

    def compute_avg(self):
        return sum(value for ts, value in self.q) / (len(self.q) or 1)


class CO2Reader:
    def __init__(self, scd30_i2c):
        i2c = adafruit_extended_bus.ExtendedI2C(discover_scd30_i2c(scd30_i2c))
        self.scd = adafruit_scd30.SCD30(i2c)

    async def read(self):
        while True:
            if not self.scd.data_available:
                await asyncio.sleep(0.5)
                continue

            co2 = self.scd.CO2
            if co2 is None or not math.isfinite(co2):
                print(f"ignored co2={co2}")
                await asyncio.sleep(0.5)
                continue

            return co2


class CO2Tracker:
    def __init__(self, blinker, scd30_i2c):
        self.blinker = blinker
        self.scd30_i2c = scd30_i2c
        self.avgr = Averager(60)
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                await self.reader_loop()
            except asyncio.CancelledError:
                return
            except Exception as e:
                print("CO2Tracker failed:", e)
                await asyncio.sleep(1.0)

    async def reader_loop(self):
        reader = CO2Reader(self.scd30_i2c)

        while True:
            co2 = await reader.read()
            now = time.monotonic()
            self.avgr.add(now, co2)

            co2_avg = self.compute_co2_avg()
            self.blinker.blink_number(co2_avg // 100)

    def compute_co2_avg(self):
        now = time.monotonic()
        if self.avgr.is_fresh(now):
            co2_avg = int(self.avgr.compute_avg())
            # Enforce reasonable limits for the blinker.
            if co2_avg < 100:
                co2_avg = 100
            if co2_avg > 2000:
                co2_avg = 2000
            return co2_avg
        else:
            return 0  # No data... this will turn off the fan.


class Blinker:
    def __init__(self):
        self.q = asyncio.Queue(maxsize=1)
        self.hz = None
        self.task = asyncio.create_task(self.run())

    def blink_number(self, number):
        try:
            self.q.put_nowait(number)
        except asyncio.QueueFull:
            pass
        self.hz = None

    def blink_hz(self, hz):
        self.hz = hz

    async def run(self):
        try:
            await self.run_or_die()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            traceback.print_exc()
            raise

    async def run_or_die(self):
        # Raspberry Pi red LED:
        #   chmod a+w /sys/class/leds/led1/brightness
        #   ln -s /sys/class/leds/led1/brightness /tmp/exhale.led
        # Le Potato green LED:
        #   chmod a+w /sys/class/leds/librecomputer:system-status/brightness
        #   ln -s /sys/class/leds/librecomputer:system-status/brightness /tmp/exhale.led
        with open("/tmp/exhale.led", "w") as f:
            async def write_n(n, sleep_time=None):
                f.write("%d\n" % n)
                f.flush()
                if sleep_time is not None:
                    await asyncio.sleep(sleep_time)
            while True:
                if self.hz:
                    # Blink continuously.
                    await write_n(1, 0.5 / self.hz)
                    await write_n(0, 0.5 / self.hz)
                    continue

                number = await self.q.get()
                #print(f"blink {number}")
                self.q.task_done()
                for i in range(number):
                    if (i + 1) % 5 == 0:
                        await write_n(0, 0.2)
                        await write_n(1, 0.3)
                        await write_n(0, 0.0)
                    else:
                        await write_n(0, 0.2)
                        await write_n(1, 0.1)
                        await write_n(0, 0.2)
                await asyncio.sleep(3.0)


def discover_zdevice(zdevice):
    if zdevice is not None:
        return zdevice
    GLOBS = [
        # Le Potato "ldto enable uarta" -- not sure where 6 comes from.
        "/dev/ttyAML6",
        # RasPi "raspi-config nonint do_serial 2"
        "/dev/ttyS0",
    ]
    for g in GLOBS:
        for zdevice in glob.glob(g):
            print(f"Discovered --zdevice={zdevice}")
            return zdevice


def discover_scd30_i2c(scd30_i2c):
    if scd30_i2c is not None:
        return scd30_i2c
    GLOBS = [
        # Raspberry Pi dtoverlay=i2c-gpio,bus=302,...
        "/dev/i2c-302",
        # lepotato/i2c-exhale.dts
        "/sys/devices/platform/i2c-exhale/i2c-*",
    ]
    for g in GLOBS:
        for path in glob.glob(g):
            m = re.search("[0-9]+$", path)
            if not m:
                continue
            scd30_i2c = int(m.group(0))
            print(f"Discovered --scd30_i2c={scd30_i2c} from {path}")
            return scd30_i2c
    raise ValueError("Failed to discover --scd30_i2c")


async def hard_reset(args):
    def manager_set_value(switch_id, value):
        print("ignored manager_set_value")

    manual_secs = 60  # value is irrelevant
    st = StateTracker(manager_set_value, manual_secs)

    manager = libopenzwave.PyManager()
    manager.create()
    manager.addWatcher(st.threadsafe_watcher_cb)
    manager.addDriver(discover_zdevice(args.zdevice))

    await st.wait_for_nodes()

    # This seems necessary since openzwave added a bunch of ZWave+ junk.
    print("Sleeping for 10 seconds...")
    await st.wait_until(time.monotonic() + 10)

    print("Resetting controller...")
    manager.resetController(st.home_id)
    await st.wait_for_driver_removed()
    await st.wait_for_nodes()

    for i in range(args.switches):
        print("Adding node...")
        manager.addNode(st.home_id, doSecurity=False)
        await st.wait_for_controller_state("Waiting")
        print(f"\n!!! Please add switch #{i+1} of {args.switches}.\nAssuming you have an UltraPro Z-Wave toggle switch in the factory-reset state, just press 'up'.\n")

        switch_id = await st.wait_for_switch_added()
        # Acknowledge the new switch, by turning it off.
        manager.setValue(switch_id, False)

    print("Sleeping for 5 seconds...")
    await st.wait_until(time.monotonic() + 5)
    print("Destroying...")
    manager.destroy()
    print("Done!")


async def calibrate(args):
    blinker = Blinker()
    reader = CO2Reader(args.scd30_i2c)

    target = time.monotonic() + 120  # calibrate in 2 minutes
    blinker.blink_hz(0.5)  # blink slowly
    ppm = args.scd30_ppm
    info = f"(scd30_ppm={ppm})" if ppm else "(dry_run)"

    print("SCD30 calibration mode")

    while True:
        co2 = int(await reader.read())
        remain = max(0, math.ceil(target - time.monotonic()))
        print(f"co2={co2}, calibrate in {remain}s {info}")
        if remain <= 0:
            break

    print("Calibrating!")
    if ppm:
        reader.scd.self_calibration_enabled = False
        reader.scd.forced_recalibration_reference = ppm
    blinker.blink_hz(5)  # blink quickly

    while True:
        co2 = int(await reader.read())
        print(f"co2={co2}, calibrated {info}")


async def co2_main(args):
    blinker = Blinker()
    co2_tracker = CO2Tracker(blinker, args.scd30_i2c)
    try:
        await co2_sub(args, co2_tracker)
    finally:
        blinker.task.cancel()
        co2_tracker.task.cancel()

async def co2_sub(args, co2_tracker):
    boot_time = time.monotonic()

    def manager_set_value(switch_id, value):
        manager.setValue(switch_id, value)

    st = StateTracker(manager_set_value, args.manual)

    manager = libopenzwave.PyManager()
    manager.create()
    manager.addWatcher(st.threadsafe_watcher_cb)
    manager.addDriver(discover_zdevice(args.zdevice))

    await st.wait_for_nodes()
    print("Active switch count: %d" % len(st.switches))

    # Useful for detecting when a switch is dead.
    manager.setPollInterval(10000, True)
    for switch in st.switches.values():
        manager.enablePoll(switch.switch_id)

    fan = FanState.OFF

    duty_1h_avgr = Averager(1*3600)
    duty_24h_avgr = Averager(24*3600)
    last_uniq = None

    while True:
        co2 = co2_tracker.compute_co2_avg()
        if co2 >= args.co2_limit:
            fan = FanState.ON
        elif co2 <= args.co2_limit - args.co2_diff:
            fan = FanState.OFF

        for switch in st.switches.values():
            switch.set_want_onoff(fan.value)

        now = time.monotonic()
        duty_1h_avgr.add(now, fan.value)
        duty_24h_avgr.add(now, fan.value)

        # Round up, so any activity reports >= 1%
        duty_1h = math.ceil(duty_1h_avgr.compute_avg() * 100)
        duty_24h = math.ceil(duty_24h_avgr.compute_avg() * 100)

        # Log every 5 minutes, or when fan state changes.
        dt_now = datetime.datetime.now()
        dt_5min = dt_now - datetime.timedelta(
                minutes=dt_now.minute % 5,
                seconds=dt_now.second,
                microseconds=dt_now.microsecond)

        uniq = (dt_5min, fan)
        if uniq != last_uniq:
            dt_nowf = dt_now.replace(microsecond=0).isoformat(sep=" ")
            uptime = int((now - boot_time) // 3600)
            print(f"{dt_nowf} co2={co2} fan={fan.name} "
                  f"uptime={uptime}h duty_1h={duty_1h}% duty_24h={duty_24h}%",
                  flush=True)
            last_uniq = uniq

        # Passively consume messages for ~10 seconds.
        await st.wait_until(now + 137/13)


# https://stackoverflow.com/questions/20094215/argparse-subparser-monolithic-help-output
class _HelpAction(argparse._HelpAction):
    def __call__(self, parser, namespace, values, option_string=None):
        subparsers_actions = [
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)]
        for subparsers_action in subparsers_actions:
            # get all subparsers and print help
            for choice, subparser in subparsers_action.choices.items():
                print(f"=== subcommand '{choice}' ===")
                subparser.print_help()
                print()
        parser.exit()


def main():
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="subcommand")
    parser.add_argument('-h', '--help', action=_HelpAction)

    p = subparsers.add_parser("calibrate", description="Calibrate the SCD30 CO₂ sensor in outdoor air. LED will blink slow for 2 minutes, calibrate, then blink quickly. Without --scd30_ppm, this just tests the sensor.")
    p.add_argument("--scd30_i2c", type=int, help="Read from SCD30 at /dev/i2c-N (default=auto)", metavar="N")
    p.add_argument("--scd30_ppm", type=int, help="Outdoor CO₂ ppm (default=dry_run)", default=None, metavar="PPM")
    p.set_defaults(func=calibrate)

    p = subparsers.add_parser("reset", description="Reinitialize the ZWave network. Before running this command, all switches must be in the 'factory reset' state. To factory reset an UltraPro Z-Wave toggle switch, quickly press 'up up up down down down'. Later when prompted, press 'up' to add each switch to the ZWave network.")
    p.add_argument("--zdevice", help="ZWave serial device (default=auto)", metavar="/dev/ttyX")
    p.add_argument("--switches", type=int, help="Number of switches to add", required=True, metavar="N")
    p.set_defaults(func=hard_reset)

    p = subparsers.add_parser("run", description="Run the daemon to monitor CO₂ levels and control exhaust fans.")
    p.add_argument("--zdevice", help="ZWave serial device (default=auto)", metavar="/dev/ttyX")
    p.add_argument("--scd30_i2c", type=int, help="Read from SCD30 at /dev/i2c-N (default=auto)", metavar="N")
    p.add_argument("--co2_limit", type=int, help="Enable fan when CO₂ level exceeds this ppm value", default=900, metavar="900")
    p.add_argument("--co2_diff", type=int, help="Disable fan when CO₂ level falls below (limit-diff)", default=50, metavar="50")
    p.add_argument("--manual", type=int, help="When a switch is toggled manually, disable automatic control for this many seconds", default=3600, metavar="3600")
    p.set_defaults(func=co2_main)

    args = parser.parse_args()
    if not args.subcommand:
        return _HelpAction(None)(parser, None, None)

    with tempfile.TemporaryDirectory(prefix="exhale-userpath-") as user_path:
        # Log here to get console output without colors.
        os.symlink("/dev/stdout", os.path.join(user_path, "stdout.log"))

        options = libopenzwave.PyOptions(user_path=user_path)
        options.addOptionString("LogFileName", "stdout.log", False)
        options.addOptionInt("SaveLogLevel", 4)  # 4=Error
        options.addOptionBool("ConsoleOutput", False)
        options.addOptionBool("AutoUpdateConfigFile", False)
        options.lock()

        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
